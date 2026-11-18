package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

// snapshot_file: the bytes write_config is about to replace (#853).
//
// The read tier redacts a unit file's Environment= values and Exec*= arguments,
// so read_file can no longer give a rollback the bytes it must put back - a
// revoke of nearly every unit an artifact overwrote would have to answer
// "cannot put back". This is the WRITE path's own read instead, with the write
// path's authority and no more:
//
//   - privileged agents only, exactly like write_config (an unprivileged agent
//     cannot apply the change the snapshot is for);
//   - only a path write_config could replace (the write allowlist, resolved
//     parent, never the agent's own state files), and never a path the read
//     policy denies (keys, credentials, *.env, ...);
//   - never through a symlink: write_config replaces the link itself, so the
//     link's target is not what a rollback restores.
//
// The control plane asks for it only while capturing the pre-apply state of an
// artifact it is about to apply (executor/host_provision.py); no MCP tool and
// no HTTP route reaches it. The bytes are kept with the artifact's capture for
// its revoke, as they were before the read tier redacted.
func snapshotFile(privileged bool, path string) (string, error) {
	if !privileged {
		return "", fmt.Errorf("snapshot_file requires privileged mode")
	}
	resolved, _, err := resolveWritePath(path)
	if err != nil {
		return "", err
	}
	for _, p := range []string{filepath.Clean(path), resolved} {
		if denied, reason := isDenied(p); denied {
			return "", fmt.Errorf("%s", reason)
		}
	}
	f, err := os.OpenFile(resolved, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		switch {
		case os.IsNotExist(err):
			return "", fmt.Errorf("file not found: %s", path)
		case errors.Is(err, syscall.ELOOP):
			return "", fmt.Errorf("is a symbolic link; write_config replaces the link, so its target is not snapshotted: %s", path)
		case os.IsPermission(err):
			return "", fmt.Errorf("permission denied: %s", path)
		}
		return "", err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("not a regular file: %s", path)
	}
	if info.Size() > maxPayloadBytes {
		return "", fmt.Errorf("file is %d bytes; the agent hub accepts at most %d per reply: %s",
			info.Size(), maxPayloadBytes, path)
	}
	data, err := io.ReadAll(io.LimitReader(f, maxPayloadBytes+1))
	if err != nil {
		return "", err
	}
	if len(data) > maxPayloadBytes {
		return "", fmt.Errorf("file grew past %d bytes while it was read: %s", maxPayloadBytes, path)
	}
	return string(data), nil
}
