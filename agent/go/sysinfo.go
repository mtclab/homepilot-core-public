package main

import (
	"bufio"
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"
)

// collectSystemInfo mirrors hp_agent.system_info.collect_system_info.
func collectSystemInfo() map[string]any {
	host, _ := os.Hostname()
	info := map[string]any{
		"hostname": host,
		"os":       "Linux",
		// The agent's OWN build stamp, alongside the kernel's. Reported on every
		// register, so the hub can answer "which hosts still run the broken
		// binary" from the fleet list instead of an SSH sweep (#430).
		"agent_version": agentVersion(),
		"os_version":    kernelRelease(),
		"arch":          runtime.GOARCH,
		"runtime":       "go " + runtime.Version(),
		"cpu_count":     runtime.NumCPU(),
		"disk":          diskInfo(),
		"memory":        memInfo(),
		"load":          loadInfo(),
	}
	return info
}

func kernelRelease() string {
	// /proc instead of syscall.Uname — Utsname char type differs by arch
	// (int8 amd64 / uint8 arm64), which breaks cross-compilation.
	data, err := os.ReadFile("/proc/sys/kernel/osrelease")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func round2(f float64) float64 {
	return float64(int64(f*100+0.5)) / 100
}

func diskInfo() map[string]float64 {
	var st syscall.Statfs_t
	if err := syscall.Statfs("/", &st); err != nil {
		return map[string]float64{}
	}
	total := float64(st.Blocks) * float64(st.Bsize)
	free := float64(st.Bavail) * float64(st.Bsize)
	return map[string]float64{
		"total_gb": round2(total / 1e9),
		"free_gb":  round2(free / 1e9),
	}
}

func memInfo() map[string]float64 {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return map[string]float64{}
	}
	defer f.Close()
	vals := map[string]int64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		fields := strings.Fields(v) // "  16384256 kB"
		if len(fields) == 0 {
			continue
		}
		n, err := strconv.ParseInt(fields[0], 10, 64)
		if err == nil {
			vals[strings.TrimSpace(k)] = n
		}
	}
	totalKB := vals["MemTotal"]
	freeKB := vals["MemAvailable"]
	return map[string]float64{
		"total_gb": round2(float64(totalKB) / 1e6),
		"free_gb":  round2(float64(freeKB) / 1e6),
	}
}

func loadInfo() map[string]float64 {
	data, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return map[string]float64{}
	}
	fields := strings.Fields(string(data))
	if len(fields) < 3 {
		return map[string]float64{}
	}
	parse := func(s string) float64 {
		v, _ := strconv.ParseFloat(s, 64)
		return round2(v)
	}
	return map[string]float64{
		"load_1m":  parse(fields[0]),
		"load_5m":  parse(fields[1]),
		"load_15m": parse(fields[2]),
	}
}
