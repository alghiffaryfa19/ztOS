// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * ZrnDevTest: what the phone is doing, for whoever is bringing it up. System
 * state, the sensor readings this kernel exposes as they change, and the
 * systemd journal, the kernel log and the shell's own log as they are written.
 * Built on ZrnMetroCore; nothing here draws a window or handles a finger.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/statvfs.h>
#include <sys/sysinfo.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#include "zrnmetrocore.h"

enum page { PAGE_HUB, PAGE_SYSTEM, PAGE_SENSORS, PAGE_JOURNAL, PAGE_KERNEL, PAGE_SHELL, PAGE_HARDWARE,
	PAGE_LOGS /* the hub's second tab */ };
enum action { ACT_OPEN = 1 };

#define MAX_LINES	70	/* of a log, newest first */
#define LINE_LEN	240
#define BATTERY		"/sys/class/power_supply/max170xx_battery/"
#define CHARGER		"/sys/class/power_supply/max77705-charger/"

static struct {
	char lines[MAX_LINES][LINE_LEN];
	int n, head;		/* a ring: head is where the next line goes */
	char partial[LINE_LEN];
	size_t partial_len;
	pid_t pid;
	int fd;
} stream = { .fd = -1 };

static void stream_stop(void) {
	if (stream.fd >= 0) {
		zmc_watch_fd(stream.fd, NULL, NULL);
		close(stream.fd);
		stream.fd = -1;
	}
	if (stream.pid > 0) {
		kill(stream.pid, SIGTERM);
		waitpid(stream.pid, NULL, 0);
		stream.pid = 0;
	}
	zmc_busy(false);
}

static void stream_line(const char *line) {
	snprintf(stream.lines[stream.head], LINE_LEN, "%s", line);
	stream.head = (stream.head + 1) % MAX_LINES;
	if (stream.n < MAX_LINES) {
		stream.n++;
	}
}

static void stream_ready(int fd, void *data) {
	char buf[4096];
	ssize_t got = read(fd, buf, sizeof(buf));
	if (got <= 0) {
		stream_stop();
		stream_line("(the log ended)");
		zmc_redraw();
		return;
	}
	for (ssize_t i = 0; i < got; i++) {
		if (buf[i] == '\n') {
			stream.partial[stream.partial_len] = 0;
			stream_line(stream.partial);
			stream.partial_len = 0;
		} else if (stream.partial_len < LINE_LEN - 1) {
			stream.partial[stream.partial_len++] = buf[i];
		}
	}
	zmc_busy(false);	/* the first lines are here */
	zmc_redraw();
}

/* Run a command and collect what it prints, line by line, for as long as
 * its page is open. */
static void stream_start(const char *command) {
	int fds[2];
	stream_stop();
	stream.n = stream.head = 0;
	stream.partial_len = 0;
	if (pipe2(fds, O_CLOEXEC) < 0) {
		return;
	}
	stream.pid = fork();
	if (stream.pid == 0) {
		dup2(fds[1], STDOUT_FILENO);
		dup2(fds[1], STDERR_FILENO);
		execl("/bin/sh", "sh", "-c", command, (char *)NULL);
		_exit(127);
	}
	close(fds[1]);
	stream.fd = fds[0];
	fcntl(stream.fd, F_SETFL, O_NONBLOCK);
	zmc_watch_fd(stream.fd, stream_ready, NULL);
	zmc_busy(true);
}

static double draw_stream(cairo_t *cr, const char *title) {
	double y = zmc_header(cr, title);
	if (stream.n == 0) {
		cairo_set_source_rgb(cr, 0.6, 0.6, 0.6);
		zmc_text(cr, "waiting for the first lines", FONT_REGULAR, 13.5, ZMC_MARGIN, y, 0);
		return y + 40;
	}
	for (int i = 0; i < stream.n; i++) {
		const char *line = stream.lines[(stream.head - 1 - i + 2 * MAX_LINES) % MAX_LINES];
		/* the newest is at the top, and brighter than what is behind it */
		cairo_set_source_rgb(cr, i == 0 ? 1.0 : 0.72, i == 0 ? 1.0 : 0.72, i == 0 ? 1.0 : 0.72);
		y += zmc_text(cr, line, FONT_MONO, 9.5, ZMC_MARGIN, y, zmc.width - 2 * ZMC_MARGIN) + 5;
	}
	return y + 20;
}

static int hub_tab = PAGE_HUB;	/* the hub tab last shown */

static double draw_hub(cairo_t *cr) {
	double y = zmc_pivot_next(cr, "devtest", "logs", ACT_OPEN, PAGE_LOGS);
	y = zmc_entry(cr, y, "system", "kernel, memory, storage, load", ACT_OPEN, PAGE_SYSTEM);
	y = zmc_entry(cr, y, "sensors", "battery, charger, heat, clocks: live", ACT_OPEN, PAGE_SENSORS);
	y = zmc_entry(cr, y, "hardware", "what this kernel drives, and what it does not", ACT_OPEN, PAGE_HARDWARE);
	return y + 20;
}

/* The hub's second tab: the logs, as they are written. */
static double draw_logs(cairo_t *cr) {
	double y = zmc_pivot_next(cr, "logs", "devtest", ACT_OPEN, PAGE_HUB);
	y = zmc_entry(cr, y, "journal", "systemd's log as it is written", ACT_OPEN, PAGE_JOURNAL);
	y = zmc_entry(cr, y, "kernel log", "the kernel's messages as they come", ACT_OPEN, PAGE_KERNEL);
	y = zmc_entry(cr, y, "shell log", "ztMETRO's own log", ACT_OPEN, PAGE_SHELL);
	return y + 20;
}

static double draw_system(cairo_t *cr) {
	char value[256], a[96];
	double y = zmc_header(cr, "system");
	struct utsname uts;
	if (uname(&uts) == 0) {
		snprintf(value, sizeof(value), "%s %s (%s)", uts.sysname, uts.release, uts.machine);
		y = zmc_field(cr, y, "Kernel", value);
	}
	zmc_read_file("/sys/firmware/devicetree/base/model", a, sizeof(a));
	y = zmc_field(cr, y, "Device tree model", a[0] ? a : "unknown");
	struct sysinfo si;
	if (sysinfo(&si) == 0) {
		snprintf(value, sizeof(value), "%ld h %ld min", si.uptime / 3600, (si.uptime % 3600) / 60);
		y = zmc_field(cr, y, "Uptime", value);
		snprintf(value, sizeof(value), "%.0f MB free of %.0f MB, %.0f MB swap used",
			si.freeram * (double)si.mem_unit / 1e6, si.totalram * (double)si.mem_unit / 1e6,
			(si.totalswap - si.freeswap) * (double)si.mem_unit / 1e6);
		y = zmc_field(cr, y, "Memory", value);
		snprintf(value, sizeof(value), "%d processes", si.procs);
		y = zmc_field(cr, y, "Tasks", value);
	}
	zmc_read_file("/proc/loadavg", a, sizeof(a));
	y = zmc_field(cr, y, "Load", a);
	struct statvfs fs;
	if (statvfs("/", &fs) == 0) {
		snprintf(value, sizeof(value), "%.1f GB free of %.1f GB",
			(double)fs.f_bavail * fs.f_frsize / 1e9, (double)fs.f_blocks * fs.f_frsize / 1e9);
		y = zmc_field(cr, y, "Root file system", value);
	}
	zmc_read_file("/proc/cmdline", value, sizeof(value));
	y = zmc_field(cr, y, "Kernel command line", value);
	snprintf(value, sizeof(value), "%s on %s", getenv("XDG_SESSION_TYPE") ? getenv("XDG_SESSION_TYPE") : "?",
		getenv("WAYLAND_DISPLAY") ? getenv("WAYLAND_DISPLAY") : "?");
	y = zmc_field(cr, y, "Session", value);
	return y + 10;
}

static long read_long(const char *path) {
	char text[64];
	zmc_read_file(path, text, sizeof(text));
	return text[0] ? atol(text) : 0;
}

static double draw_sensors(cairo_t *cr) {
	char value[256], a[64], b[64];
	double y = zmc_header(cr, "sensors");
	zmc_read_file(BATTERY "status", a, sizeof(a));
	snprintf(value, sizeof(value), "%ld %%, %s", read_long(BATTERY "capacity"), a);
	y = zmc_field(cr, y, "Battery", value);
	snprintf(value, sizeof(value), "%.3f V, %+.0f mA now, %+.0f mA average",
		read_long(BATTERY "voltage_now") / 1e6, read_long(BATTERY "current_now") / 1e3,
		read_long(BATTERY "current_avg") / 1e3);
	y = zmc_field(cr, y, "Cell", value);
	snprintf(value, sizeof(value), "%.1f C", read_long(BATTERY "temp") / 10.0);
	y = zmc_field(cr, y, "Battery temperature", value);
	zmc_read_file(CHARGER "status", a, sizeof(a));
	zmc_read_file(CHARGER "charge_type", b, sizeof(b));
	snprintf(value, sizeof(value), "%s (%s), input limit %ld mA, charge %ld mA", a, b,
		read_long(CHARGER "input_current_limit") / 1000, read_long(CHARGER "constant_charge_current") / 1000);
	y = zmc_field(cr, y, "Charger", value);

	for (int zone = 0; zone < 12; zone++) {
		char path[96], caption[96];
		snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%d/type", zone);
		zmc_read_file(path, a, sizeof(a));
		if (!a[0]) {
			break;
		}
		snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%d/temp", zone);
		snprintf(caption, sizeof(caption), "Heat: %s", a);
		snprintf(value, sizeof(value), "%.1f C", read_long(path) / 1000.0);
		y = zmc_field(cr, y, caption, value);
	}

	zmc_read_file("/sys/devices/system/cpu/online", a, sizeof(a));
	snprintf(value, sizeof(value), "little %ld MHz, big %ld MHz, cores %s",
		read_long("/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq") / 1000,
		read_long("/sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq") / 1000, a);
	y = zmc_field(cr, y, "Processor clocks", value);
	zmc_read_file("/sys/class/devfreq/17500000.gpu/governor", a, sizeof(a));
	snprintf(value, sizeof(value), "%ld MHz (%s)", read_long("/sys/class/devfreq/17500000.gpu/cur_freq") / 1000000, a);
	y = zmc_field(cr, y, "GPU clock", value);

	/* whatever the kernel has under IIO: light, motion, pressure */
	DIR *dir = opendir("/sys/bus/iio/devices");
	struct dirent *entry;
	int found = 0;
	while (dir && (entry = readdir(dir))) {
		char path[320];
		if (strncmp(entry->d_name, "iio:device", 10) != 0) {
			continue;
		}
		snprintf(path, sizeof(path), "/sys/bus/iio/devices/%s/name", entry->d_name);
		zmc_read_file(path, a, sizeof(a));
		y = zmc_field(cr, y, "IIO sensor", a[0] ? a : entry->d_name);
		found++;
	}
	if (dir) {
		closedir(dir);
	}
	/* the sensor hub, through ztoscsf-sensord; the watches run out by themselves */
	static const char *const hub[][2] = {
		{ "light", "Light: lux, colour K, red green blue clear" }, { "accel", "Accelerometer: x y z" },
		{ "gyro", "Gyroscope: x y z" }, { "pressure", "Pressure (raw)" }, { "prox", "Proximity" },
	};
	int ctl = open("/run/ztoscsf-sensord.ctl", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	for (int i = 0; i < 5; i++) {
		char path[64];
		if (ctl >= 0 && i > 0) {
			dprintf(ctl, "watch %s\n", hub[i][0]);
		}
		snprintf(path, sizeof(path), "/run/ztoscsf-sensors/%s", hub[i][0]);
		zmc_read_file(path, a, sizeof(a));
		if (a[0]) {
			y = zmc_field(cr, y, hub[i][1], a);
			found++;
		}
	}
	if (ctl >= 0) {
		close(ctl);
	}
	if (!found) {
		y = zmc_field(cr, y, "Sensor hub", "no readings: ztoscsf-sensord is not running");
	}
	return y + 10;
}

static bool exists(const char *path) {
	return access(path, F_OK) == 0;
}

static double draw_hardware(cairo_t *cr) {
	static const struct { const char *what, *path, *missing; } parts[] = {
		{ "Display (DECON, DSI)", "/sys/class/drm/card0-DSI-1", "no panel" },
		{ "GPU (Mali-G72, Panfrost)", "/sys/class/devfreq/17500000.gpu", "no GPU" },
		{ "Touch (s6sy761)", "/sys/class/input/input1", "no touch" },
		{ "Wi-Fi (brcmfmac)", "/sys/class/net/wlp1s0", "no Wi-Fi interface" },
		{ "Battery gauge (MAX77705)", "/sys/class/power_supply/max170xx_battery", "no gauge" },
		{ "Sound card", "/proc/asound/cards", "none: no codec or audio DSP driver" },
		{ "Backlight device", "/sys/class/backlight/s9p-panel", "none: brightness is done by the shell" },
		{ "Light and motion sensors", "/sys/bus/iio/devices/iio:device0", "none: no sensor hub driver" },
		{ "Vibrator", "/sys/class/leds/vibrator", "none: no haptics driver" },
		{ "Modem", "/sys/class/net/wwan0", "none: no modem driver" },
		{ "Bluetooth", "/sys/class/bluetooth/hci0", "none" },
		{ "Camera", "/dev/video0", "none" },
		{ "Processor idle states", "/sys/devices/system/cpu/cpu0/cpuidle", "none: cores only wait for interrupts" },
	};
	double y = zmc_header(cr, "hardware");
	for (size_t i = 0; i < sizeof(parts) / sizeof(parts[0]); i++) {
		y = zmc_field(cr, y, parts[i].what, exists(parts[i].path) ? "present" : parts[i].missing);
	}
	return y + 10;
}

static double render(cairo_t *cr, int page) {
	switch (page) {
	case PAGE_HUB: return draw_hub(cr);
	case PAGE_LOGS: return draw_logs(cr);
	case PAGE_SYSTEM: return draw_system(cr);
	case PAGE_SENSORS: return draw_sensors(cr);
	case PAGE_JOURNAL: return draw_stream(cr, "journal");
	case PAGE_KERNEL: return draw_stream(cr, "kernel log");
	case PAGE_SHELL: return draw_stream(cr, "shell log");
	case PAGE_HARDWARE: return draw_hardware(cr);
	}
	return 0;
}

static void activate(int action, int arg) {
	if (action == ACT_OPEN) {
		if (arg == PAGE_HUB || arg == PAGE_LOGS) {
			hub_tab = arg;
		}
		zmc_go(arg, 1);
	}
}

static void tick(void) {
	zmc_redraw();
}

static void arrived(int page) {
	if (page == PAGE_HUB || page == PAGE_LOGS) {
		hub_tab = page;	/* swiped to: where back returns to from under it */
	}
	switch (page) {
	case PAGE_SENSORS:
		zmc_every(1000, tick);	/* only while this page is open */
		break;
	case PAGE_JOURNAL:
		stream_start("exec journalctl -f -n 40 -o short --no-pager 2>&1");
		break;
	case PAGE_KERNEL:
		stream_start("exec journalctl -k -f -n 40 -o short-monotonic --no-pager 2>&1");
		break;
	case PAGE_SHELL:
		stream_start("exec tail -n 40 -F \"$HOME/.local/state/ztmetro.log\" 2>&1");
		break;
	default:
		break;
	}
}

static bool back(void) {
	if (zmc.page == PAGE_HUB || zmc.page == PAGE_LOGS) {
		return false;	/* either tab of the hub: back leaves the app */
	}
	zmc_every(0, NULL);
	stream_stop();
	zmc_go(hub_tab, -1);
	return true;
}

static const struct zmc_app devtest = {
	.app_id = "zrndevtest", .title = "ZrnDevTest", .caps = "ZRNDEVTEST",
	.render = render, .activate = activate, .back = back, .arrived = arrived,
};

int main(int argc, char **argv) {
	static const char *names[] = { "hub", "system", "sensors", "journal", "kernel", "shell", "hardware" };
	if (argc == 4 && strcmp(argv[1], "--render") == 0) {
		for (int i = 0; i < 7; i++) {
			if (strcmp(names[i], argv[2]) == 0) {
				return zmc_render_png(&devtest, i, 664, argv[3]);
			}
		}
		return 2;
	}
	int status = zmc_run(&devtest, PAGE_HUB);
	stream_stop();
	return status;
}
