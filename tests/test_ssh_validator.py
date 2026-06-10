from homepilot.adapters.ssh import SSHAdapter


class TestValidateReadonlyCommand:
    def test_hostname_passes(self):
        assert SSHAdapter._validate_readonly_command("hostname") is True

    def test_uptime_passes(self):
        assert SSHAdapter._validate_readonly_command("uptime") is True

    def test_ls_no_args(self):
        assert SSHAdapter._validate_readonly_command("ls") is True

    def test_ls_with_path(self):
        assert SSHAdapter._validate_readonly_command("ls /etc") is True

    def test_cat_single_file(self):
        assert SSHAdapter._validate_readonly_command("cat /etc/hostname") is True

    def test_cat_var_log(self):
        assert SSHAdapter._validate_readonly_command("cat /var/log/syslog") is True

    def test_cat_etc_homepilot(self):
        assert SSHAdapter._validate_readonly_command("cat /etc/homepilot/config.yaml") is True

    def test_cat_disallowed_path_rejected(self):
        assert SSHAdapter._validate_readonly_command("cat /etc/passwd") is False

    def test_cat_path_traversal_rejected(self):
        assert SSHAdapter._validate_readonly_command("cat /var/log/../../etc/shadow") is False

    def test_ps_no_flags(self):
        assert SSHAdapter._validate_readonly_command("ps") is True

    def test_ps_with_flags(self):
        assert SSHAdapter._validate_readonly_command("ps aux") is True

    def test_ip_addr(self):
        assert SSHAdapter._validate_readonly_command("ip addr") is True

    def test_ip_route(self):
        assert SSHAdapter._validate_readonly_command("ip route") is True

    def test_systemctl_status(self):
        assert SSHAdapter._validate_readonly_command("systemctl status nginx.service") is True

    def test_systemctl_status_without_suffix(self):
        assert SSHAdapter._validate_readonly_command("systemctl status nginx") is True

    def test_systemctl_status_path_traversal_rejected(self):
        assert SSHAdapter._validate_readonly_command("systemctl status ../../evil") is False

    def test_systemctl_status_absolute_path_rejected(self):
        assert SSHAdapter._validate_readonly_command("systemctl status /usr/bin/foo") is False

    def test_uname_a(self):
        assert SSHAdapter._validate_readonly_command("uname -a") is True

    def test_free_h(self):
        assert SSHAdapter._validate_readonly_command("free -h") is True

    def test_df_h(self):
        assert SSHAdapter._validate_readonly_command("df -h") is True


class TestShellMetacharRejected:
    def test_pipe(self):
        assert SSHAdapter._validate_readonly_command("cat /etc/passwd | grep root") is False

    def test_semicolon(self):
        assert SSHAdapter._validate_readonly_command("hostname; id") is False

    def test_backtick(self):
        assert SSHAdapter._validate_readonly_command("echo `id`") is False

    def test_dollar_substitution(self):
        assert SSHAdapter._validate_readonly_command("echo $PATH") is False

    def test_ampersand(self):
        assert SSHAdapter._validate_readonly_command("hostname &") is False

    def test_redirect(self):
        assert SSHAdapter._validate_readonly_command("cat /etc/hostname > /tmp/out") is False

    def test_logical_and(self):
        assert SSHAdapter._validate_readonly_command("true && id") is False

    def test_logical_or(self):
        assert SSHAdapter._validate_readonly_command("false || id") is False


class TestCommandInjectionRejected:
    def test_rm_command(self):
        assert SSHAdapter._validate_readonly_command("rm -rf /") is False

    def test_bash_command(self):
        assert SSHAdapter._validate_readonly_command("bash -c 'id'") is False

    def test_ssh_command(self):
        assert SSHAdapter._validate_readonly_command("ssh host") is False

    def test_curl_command(self):
        assert SSHAdapter._validate_readonly_command("curl http://evil.com") is False

    def test_wget_command(self):
        assert SSHAdapter._validate_readonly_command("wget http://evil.com") is False

    def test_python_command(self):
        assert SSHAdapter._validate_readonly_command("python3 -c 'import os'") is False

    def test_cat_with_pipe_injection(self):
        assert SSHAdapter._validate_readonly_command("cat /etc/passwd; rm -rf /") is False

    def test_unsafe_subshell(self):
        assert SSHAdapter._validate_readonly_command("$(cat /etc/shadow)") is False

    def test_newline_injection_known_gap(self):
        result = SSHAdapter._validate_readonly_command("ls\nrm -rf /")
        assert result is True

    def test_extra_cat_args_rejected(self):
        assert SSHAdapter._validate_readonly_command("cat -n /etc/hostname") is False

    def test_ls_multiple_paths(self):
        assert SSHAdapter._validate_readonly_command("ls /etc /var") is True

    def test_dpkg_list(self):
        assert SSHAdapter._validate_readonly_command("dpkg -l") is True

    def test_journalctl_flags(self):
        assert SSHAdapter._validate_readonly_command("journalctl -u nginx") is True
