package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
)

// Docker containers and compose projects (#818), read from the Engine API on
// the local socket - only when the agent's user can open it (root, or a member
// of the docker group). Decoded into structs that HOLD ONLY the reported
// fields: a container's environment, command, entrypoint and non-compose
// labels are never even parsed, let alone reported.

const (
	maxContainers    = 200
	maxPortsPerCtr   = 50
	maxMountsPerCtr  = 50
	dockerAPITimeout = 5 * time.Second
)

// errDockerNoSocket is "there is no Docker on this host".
var errDockerNoSocket = errors.New("no docker socket")

// dockerClient GETs Engine API paths over root's docker socket.
func dockerClient(root string) func(ctx context.Context, path string) ([]byte, error) {
	return func(ctx context.Context, path string) ([]byte, error) {
		sock := ""
		for _, p := range []string{"var/run/docker.sock", "run/docker.sock"} {
			candidate := filepath.Join(root, p)
			if _, err := os.Stat(candidate); err == nil {
				sock = candidate
				break
			}
		}
		if sock == "" {
			return nil, errDockerNoSocket
		}
		client := &http.Client{
			Timeout: dockerAPITimeout,
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					var d net.Dialer
					return d.DialContext(ctx, "unix", sock)
				},
			},
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker"+path, nil)
		if err != nil {
			return nil, err
		}
		resp, err := client.Do(req)
		if err != nil {
			if errors.Is(err, syscall.EACCES) || errors.Is(err, os.ErrPermission) {
				return nil, os.ErrPermission
			}
			return nil, err
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(io.LimitReader(resp.Body, 4*describeFileCap))
		if err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("docker API answered %d", resp.StatusCode)
		}
		return body, nil
	}
}

type dockerPort struct {
	IP          string `json:"ip,omitempty"`
	PrivatePort int    `json:"container_port"`
	PublicPort  int    `json:"host_port,omitempty"`
	Type        string `json:"proto"`
}

type dockerMount struct {
	Type        string `json:"type"`
	Source      string `json:"source,omitempty"`
	Name        string `json:"volume,omitempty"`
	Destination string `json:"destination"`
	RW          bool   `json:"rw"`
}

type containerInfo struct {
	Name           string        `json:"name"`
	Image          string        `json:"image"`
	ImageID        string        `json:"image_id,omitempty"`
	Digest         string        `json:"digest,omitempty"`
	State          string        `json:"state"`
	RestartCount   int           `json:"restart_count"`
	RestartPolicy  string        `json:"restart_policy,omitempty"`
	Ports          []dockerPort  `json:"ports"`
	Mounts         []dockerMount `json:"mounts"`
	ComposeProject string        `json:"compose_project,omitempty"`
	ComposeService string        `json:"compose_service,omitempty"`
}

type composeProject struct {
	Name        string   `json:"name"`
	WorkingDir  string   `json:"working_dir,omitempty"`
	ConfigFiles []string `json:"config_files,omitempty"`
	Services    []string `json:"services"`
}

type dockerSection struct {
	Status     string           `json:"status"`
	Reason     string           `json:"reason,omitempty"`
	Containers []containerInfo  `json:"containers"`
	Compose    []composeProject `json:"compose_projects"`
}

// The Engine API shapes, reduced to what is reported. Env, Cmd, Entrypoint and
// Config are deliberately absent.
type apiContainer struct {
	ID      string            `json:"Id"`
	Names   []string          `json:"Names"`
	Image   string            `json:"Image"`
	ImageID string            `json:"ImageID"`
	State   string            `json:"State"`
	Labels  map[string]string `json:"Labels"`
	Ports   []struct {
		IP          string `json:"IP"`
		PrivatePort int    `json:"PrivatePort"`
		PublicPort  int    `json:"PublicPort"`
		Type        string `json:"Type"`
	} `json:"Ports"`
	Mounts []struct {
		Type        string `json:"Type"`
		Name        string `json:"Name"`
		Source      string `json:"Source"`
		Destination string `json:"Destination"`
		RW          bool   `json:"RW"`
	} `json:"Mounts"`
}

type apiInspect struct {
	RestartCount int `json:"RestartCount"`
	HostConfig   struct {
		RestartPolicy struct {
			Name string `json:"Name"`
		} `json:"RestartPolicy"`
	} `json:"HostConfig"`
}

type apiImage struct {
	ID          string   `json:"Id"`
	RepoDigests []string `json:"RepoDigests"`
}

// The compose labels that are reported; every other label is ignored (labels
// carry anything, credentials included).
const (
	labelProject    = "com.docker.compose.project"
	labelService    = "com.docker.compose.service"
	labelWorkingDir = "com.docker.compose.project.working_dir"
	labelConfig     = "com.docker.compose.project.config_files"
)

func collectDocker(ctx context.Context, env describeEnv) dockerSection {
	empty := dockerSection{Containers: []containerInfo{}, Compose: []composeProject{}}
	raw, err := env.docker(ctx, "/containers/json?all=1")
	if err != nil {
		switch {
		case errors.Is(err, errDockerNoSocket):
			empty.Status, empty.Reason = statusNotInstalled, "no docker socket on this host"
		case errors.Is(err, os.ErrPermission):
			empty.Status = statusNotVisible
			empty.Reason = "docker: not readable - the agent's user cannot open the docker socket (add it to the docker group, or run a privileged agent)"
		default:
			empty.Status, empty.Reason = statusError, "the docker API did not answer"
		}
		return empty
	}
	var listed []apiContainer
	if err := json.Unmarshal(raw, &listed); err != nil {
		empty.Status, empty.Reason = statusError, "the docker API answered something unreadable"
		return empty
	}
	digests := map[string]string{}
	if rawImages, err := env.docker(ctx, "/images/json"); err == nil {
		var images []apiImage
		if json.Unmarshal(rawImages, &images) == nil {
			for _, im := range images {
				if len(im.RepoDigests) > 0 {
					d := im.RepoDigests[0]
					if _, after, ok := strings.Cut(d, "@"); ok {
						d = after
					}
					digests[im.ID] = d
				}
			}
		}
	}

	sec := dockerSection{Status: statusOK, Containers: []containerInfo{}, Compose: []composeProject{}}
	projects := map[string]*composeProject{}
	sort.Slice(listed, func(i, j int) bool { return firstName(listed[i].Names) < firstName(listed[j].Names) })
	if len(listed) > maxContainers {
		listed = listed[:maxContainers]
	}
	for _, c := range listed {
		info := containerInfo{
			Name:    firstName(c.Names),
			Image:   c.Image,
			ImageID: c.ImageID,
			Digest:  digests[c.ImageID],
			State:   c.State,
			Ports:   []dockerPort{},
			Mounts:  []dockerMount{},
		}
		for i, p := range c.Ports {
			if i >= maxPortsPerCtr {
				break
			}
			info.Ports = append(info.Ports, dockerPort{IP: p.IP, PrivatePort: p.PrivatePort, PublicPort: p.PublicPort, Type: p.Type})
		}
		for i, m := range c.Mounts {
			if i >= maxMountsPerCtr {
				break
			}
			mount := dockerMount{Type: m.Type, Destination: m.Destination, RW: m.RW}
			if m.Type == "volume" {
				mount.Name = m.Name
			} else {
				mount.Source = m.Source
			}
			info.Mounts = append(info.Mounts, mount)
		}
		if rawInspect, err := env.docker(ctx, "/containers/"+c.ID+"/json"); err == nil {
			var in apiInspect
			if json.Unmarshal(rawInspect, &in) == nil {
				info.RestartCount = in.RestartCount
				info.RestartPolicy = in.HostConfig.RestartPolicy.Name
			}
		}
		if project := c.Labels[labelProject]; project != "" {
			info.ComposeProject = project
			info.ComposeService = c.Labels[labelService]
			p, ok := projects[project]
			if !ok {
				p = &composeProject{Name: project, Services: []string{}}
				projects[project] = p
			}
			if wd := c.Labels[labelWorkingDir]; wd != "" {
				p.WorkingDir = wd
			}
			if cf := c.Labels[labelConfig]; cf != "" && len(p.ConfigFiles) == 0 {
				for _, f := range strings.Split(cf, ",") {
					if f = strings.TrimSpace(f); f != "" {
						p.ConfigFiles = append(p.ConfigFiles, f)
					}
				}
			}
			if info.ComposeService != "" && !contains(p.Services, info.ComposeService) {
				p.Services = append(p.Services, info.ComposeService)
			}
		}
		sec.Containers = append(sec.Containers, info)
	}
	names := make([]string, 0, len(projects))
	for n := range projects {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		sort.Strings(projects[n].Services)
		sec.Compose = append(sec.Compose, *projects[n])
	}
	return sec
}

func firstName(names []string) string {
	if len(names) == 0 {
		return ""
	}
	return strings.TrimPrefix(names[0], "/")
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
