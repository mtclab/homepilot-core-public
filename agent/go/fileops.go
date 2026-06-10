package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Mirrors hp_agent.file_ops. NOTE: read_file is intentionally NOT prefix-checked
// (only "..", existence, regular-file, readable) to match the Python agent.
// write_file IS restricted to these prefixes.
var writeAllowedPrefixes = []string{
	"/etc/homepilot/", "/opt/homepilot/", "/tmp/homepilot/",
	"/etc/systemd/system/", "/etc/docker/", "/etc/nginx/", "/etc/zabbix/",
}

func readFile(path string) (string, error) {
	if strings.Contains(path, "..") {
		return "", fmt.Errorf("path traversal forbidden: %s", path)
	}
	resolved, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(resolved)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("file not found: %s", path)
		}
		return "", err
	}
	if info.IsDir() {
		return "", fmt.Errorf("not a file: %s", path)
	}
	data, err := os.ReadFile(resolved)
	if err != nil {
		if os.IsPermission(err) {
			return "", fmt.Errorf("permission denied: %s", path)
		}
		return "", err
	}
	return string(data), nil
}

func writeFile(path, content string) error {
	if strings.Contains(path, "..") {
		return fmt.Errorf("path traversal forbidden: %s", path)
	}
	resolved, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	ok := false
	for _, pre := range writeAllowedPrefixes {
		if strings.HasPrefix(resolved, pre) {
			ok = true
			break
		}
	}
	if !ok {
		return fmt.Errorf("path not in allowed prefixes (%s): %s",
			strings.Join(writeAllowedPrefixes, ", "), path)
	}
	if err := os.MkdirAll(filepath.Dir(resolved), 0o755); err != nil {
		return err
	}
	return os.WriteFile(resolved, []byte(content), 0o644)
}
