package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReadFileTraversalBlocked(t *testing.T) {
	_, err := readFile("/etc/passwd/../../etc/shadow")
	if err == nil || !strings.Contains(err.Error(), "traversal") {
		t.Errorf("expected traversal error, got %v", err)
	}
}

func TestWriteFilePrefixBlocked(t *testing.T) {
	err := writeFile("/etc/passwd", "x")
	if err == nil || !strings.Contains(err.Error(), "allowed prefixes") {
		t.Errorf("expected prefix error, got %v", err)
	}
}

func TestWriteReadAllowedPrefix(t *testing.T) {
	p := filepath.Join("/tmp/homepilot", "go-fileops-test.txt")
	defer os.Remove(p)
	if err := writeFile(p, "hello"); err != nil {
		t.Fatalf("write failed: %v", err)
	}
	got, err := readFile(p)
	if err != nil || got != "hello" {
		t.Errorf("read-back mismatch: %q (%v)", got, err)
	}
}
