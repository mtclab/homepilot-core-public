package main

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"time"
)

type zabbixItem struct {
	Host  string `json:"host"`
	Key   string `json:"key"`
	Value any    `json:"value"`
	Clock int64  `json:"clock,omitempty"`
}

func zabbixEncode(items []zabbixItem) ([]byte, error) {
	payload, err := json.Marshal(map[string]any{
		"request": "sender data",
		"data":    items,
		"clock":   time.Now().Unix(),
	})
	if err != nil {
		return nil, err
	}
	out := make([]byte, 0, 13+len(payload))
	out = append(out, 'Z', 'B', 'X', 'D', 0x01) // ZBXD + flags
	var lr [8]byte
	binary.LittleEndian.PutUint32(lr[0:4], uint32(len(payload)))
	binary.LittleEndian.PutUint32(lr[4:8], 0) // reserved
	out = append(out, lr[:]...)
	out = append(out, payload...)
	return out, nil
}

// zabbixSend pushes items to a Zabbix trapper. Returns count sent + ok flag.
func zabbixSend(server string, port int, items []zabbixItem) (int, bool) {
	if len(items) == 0 {
		return 0, true
	}
	raw, err := zabbixEncode(items)
	if err != nil {
		return 0, false
	}
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("%s:%d", server, port), 5*time.Second)
	if err != nil {
		return 0, false
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := conn.Write(raw); err != nil {
		return 0, false
	}
	buf := make([]byte, 4096)
	if _, err := conn.Read(buf); err != nil {
		return len(items), false
	}
	return len(items), true
}

// systemInfoToMetrics mirrors ZabbixSender.system_info_to_metrics.
func systemInfoToMetrics(host string, sys map[string]any) []zabbixItem {
	now := time.Now().Unix()
	items := []zabbixItem{
		{Host: host, Key: "hp.agent.status", Value: 1, Clock: now},
	}
	add := func(key string, v any) { items = append(items, zabbixItem{Host: host, Key: key, Value: v, Clock: now}) }

	if cc, ok := sys["cpu_count"]; ok {
		add("hp.agent.cpu.count", cc)
	}
	if disk, ok := sys["disk"].(map[string]float64); ok && len(disk) > 0 {
		add("hp.agent.disk.total_gb", disk["total_gb"])
		add("hp.agent.disk.free_gb", disk["free_gb"])
	}
	if mem, ok := sys["memory"].(map[string]float64); ok && len(mem) > 0 {
		add("hp.agent.memory.total_gb", mem["total_gb"])
		add("hp.agent.memory.free_gb", mem["free_gb"])
	}
	if load, ok := sys["load"].(map[string]float64); ok && len(load) > 0 {
		add("hp.agent.load.1m", load["load_1m"])
		add("hp.agent.load.5m", load["load_5m"])
		add("hp.agent.load.15m", load["load_15m"])
	}
	return items
}
