// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * zrnperfd, the small one: power profiles for the Galaxy S9 under ztMETRO.
 *
 * The laptop's zrn_perfd watches processes and predicts load. This keeps only
 * what a phone on this kernel can use: three profiles, and a low-power state
 * for when the screen is off. It runs as root, sleeps in read() on a pipe and
 * does nothing until something is written to it:
 *
 *	profile powersave | balanced | performance
 *	deepsleep on | off	whether "screen off" lowers anything
 *	screen off | on		from the shell, as the display goes off and on
 *	focus PID APP_ID	from the shell: the app now in front (PID 0: Start)
 *	shell PID		from the shell as it starts: the compositor's pid
 *	policy PID normal | eco | big
 *				an app's own say in boost, while it is in front:
 *				eco, its load never steps boost up (work it does
 *				not mind doing slowly: the efficiency cores are
 *				enough); big, the big cores stay open to it (a
 *				game, say), at no more than BIG_SHARE of their
 *				speed until it pins them too; normal, as any app.
 *				Forgotten when that process ends.
 *	tickle battery|charging|saver none|nominal|moderate|high
 *				the tickle mode for each condition (see tickle.c):
 *				on battery, plugged in, and with the power saver
 *				profile, which wins over the other two
 *
 * /run/zrnperfd.ctl is that pipe (anyone may write), /run/zrnperfd.status
 * says what is set, /var/lib/zrnperfd/state keeps it across boots.
 *
 * There are no cpuidle states on this kernel, so "deep sleep" is both
 * clusters and the GPU held at their lowest operating points, and the big
 * cores but one offline (see dynamic boost).
 *
 * Dynamic boost (the balanced profile, screen on), in three steps, as the
 * laptop's daemon has it:
 *
 *	nominal	everything runs on the four efficiency cores; the big cluster
 *		is given nothing to do and is held at its lowest clock
 *	big	the app in front (or the shell) has used more than 95 % of a
 *		core for a moment: the big cores are opened to it, at no more
 *		than 65 % of their top speed
 *	rapid	and still more than 95 %: both clusters at their fastest clock
 *
 * It steps down again when the load has been low for a while. In nominal,
 * and with the screen off, three of the four big cores are taken offline,
 * which powers them down, and brought back for big and rapid (about 90 ms
 * for the three). That needs zrn-hotplug.ko (power/hotplug): without it a
 * core taken offline on this kernel does not come back, so then none is
 * touched and programs are only kept off the big cores with the cpuset
 * controller. One big core always stays (a whole cluster down is another
 * matter, see the module), idle and at its lowest clock.
 *
 * While rapid boost is on the front light blinks white, in the phases the
 * laptop's daemon blinks its caps lock light in: 5 Hz (100 ms on, 100 ms
 * off), and 10 Hz (50 and 50) once the load has stayed above 95 % for a
 * second with everything already flat out ("extreme"). The chip's own
 * blinking cannot go that fast (its shortest dark time is half a second), so
 * it is done from here; it only costs wake-ups while the processor is busy
 * anyway. Afterwards the light goes back to what the charger says.
 *
 * What is on the screen comes first: the shell runs at nice -10 and the app
 * in front at -5, and both ask the scheduler for at least half of a big
 * core's speed whenever they run (uclamp), so that a frame never waits for a
 * slow core or a slow clock. Idle, a clamp costs nothing.
 */
#define _GNU_SOURCE
#include "tickle.h"

#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/netlink.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <dirent.h>
#include <stdint.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define CTL	"/run/zrnperfd.ctl"
#define STATUS	"/run/zrnperfd.status"
#define STATE	"/var/lib/zrnperfd/state"
#define GPU	"/sys/class/devfreq/17500000.gpu"
#define CPUFREQ	"/sys/devices/system/cpu/cpufreq"

enum profile { POWERSAVE, BALANCED, PERFORMANCE };
static const char *names[] = { "powersave", "balanced", "performance" };
static const int clusters[] = { 0, 4 };

static enum profile profile = BALANCED;
static bool deepsleep = true, screen_on = true;
/* The Wi-Fi radio and the modem are never switched off with the screen: a phone that
 * stops receiving while it sleeps is not a phone. The Wi-Fi chip's own power save
 * (listening only for beacons) is on all the time, and ztOSCoreNetworking lowers its
 * polling with the screen. */
/* tickle mode on battery, plugged in, and in power saver */
static enum tickle_mode tickle_when[3] = { TICKLE_MODERATE, TICKLE_NOMINAL, TICKLE_HIGH };
static const char *const conditions[3] = { "battery", "charging", "saver" };
static bool plugged;

/* a cable in: any power supply other than the battery says it is online */
static bool read_plugged(void) {
	glob_t g;
	bool on = false;
	if (glob("/sys/class/power_supply/*/online", 0, NULL, &g) == 0) {
		for (size_t i = 0; i < g.gl_pathc; i++) {
			FILE *f = fopen(g.gl_pathv[i], "r");
			int v = 0;
			if (f && fscanf(f, "%d", &v) == 1 && v == 1) {
				on = true;
			}
			if (f) {
				fclose(f);
			}
		}
	}
	globfree(&g);
	return on;
}

static int condition(void) {
	return profile == POWERSAVE ? 2 : plugged ? 1 : 0;
}

/* ---- the screen first -------------------------------------------------------- */

#define SHELL_NICE	-10
#define FRONT_NICE	-5
#define UI_UCLAMP	512	/* of 1024: half a big core */

struct sched_attr_v1 {
	uint32_t size, sched_policy;
	uint64_t sched_flags;
	int32_t sched_nice;
	uint32_t sched_priority;
	uint64_t sched_runtime, sched_deadline, sched_period;
	uint32_t sched_util_min, sched_util_max;
};
#define FLAG_RESET_ON_FORK	0x01	/* what they start is not raised with them */
#define FLAG_UTIL_CLAMP_MIN	0x20

/* Every thread of `pid`: this nice, and this least utilisation when it runs. */
static void prioritise(pid_t pid, int nice, int uclamp) {
	char path[64];
	snprintf(path, sizeof(path), "/proc/%d/task", pid);
	DIR *dir = opendir(path);
	struct dirent *entry;
	while (dir && (entry = readdir(dir))) {
		pid_t tid = atoi(entry->d_name);
		if (tid <= 0) {
			continue;
		}
		/* the policy is given rather than kept: a kept policy keeps the
		 * old reset-on-fork with it */
		struct sched_attr_v1 attr = {
			.size = sizeof(attr),
			.sched_policy = 0,	/* SCHED_OTHER */
			.sched_flags = FLAG_RESET_ON_FORK | FLAG_UTIL_CLAMP_MIN,
			.sched_nice = nice,
			.sched_util_min = (uint32_t)uclamp,
		};
		if (syscall(SYS_sched_setattr, tid, &attr, 0) < 0) {
			setpriority(PRIO_PROCESS, tid, nice);
		}
	}
	if (dir) {
		closedir(dir);
	}
}

static pid_t shell_pid, front_pid;

/* what apps have asked for (policy PID MODE); few, and gone when they are */
enum policy { POLICY_NORMAL, POLICY_ECO, POLICY_BIG };
static struct { pid_t pid; enum policy mode; } policies[32];

static enum policy policy_of(pid_t pid) {
	for (int i = 0; i < 32; i++) {
		if (policies[i].pid == pid && pid > 0) {
			if (kill(pid, 0) != 0 && errno == ESRCH) {
				policies[i].pid = 0;	/* that app has ended */
				return POLICY_NORMAL;
			}
			return policies[i].mode;
		}
	}
	return POLICY_NORMAL;
}

static void policy_set(pid_t pid, enum policy mode) {
	int free_slot = -1;
	for (int i = 0; i < 32; i++) {
		if (policies[i].pid == pid) {
			policies[i].mode = mode;
			if (mode == POLICY_NORMAL) {
				policies[i].pid = 0;
			}
			return;
		}
		if (free_slot < 0 && (policies[i].pid == 0 || kill(policies[i].pid, 0) != 0)) {
			free_slot = i;
		}
	}
	if (mode != POLICY_NORMAL && free_slot >= 0) {
		policies[free_slot].pid = pid;
		policies[free_slot].mode = mode;
	}
}

static void put(const char *path, const char *value) {
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0) {
		return;
	}
	if (write(fd, value, strlen(value)) < 0) {
		fprintf(stderr, "zrnperfd: %s <- %s failed\n", path, value);
	}
	close(fd);
}

static void put_long(const char *path, long value) {
	char text[32];
	snprintf(text, sizeof(text), "%ld", value);
	put(path, text);
}

/* The operating points of a table, highest first or not: sorted here. */
static int points(const char *path, long *out, int max) {
	FILE *f = fopen(path, "r");
	int n = 0;
	while (f && n < max && fscanf(f, "%ld", &out[n]) == 1) {
		n++;
	}
	if (f) {
		fclose(f);
	}
	for (int i = 0; i < n; i++) {
		for (int j = i + 1; j < n; j++) {
			if (out[j] < out[i]) {
				long t = out[i];
				out[i] = out[j];
				out[j] = t;
			}
		}
	}
	return n;
}

/* The highest point no more than `share` of the top one. */
static long capped(const long *table, int n, double share) {
	long best = table[0];
	for (int i = 0; i < n; i++) {
		if (table[i] <= table[n - 1] * share) {
			best = table[i];
		}
	}
	return best;
}

/* ---- dynamic boost ----------------------------------------------------------- */

enum boost { NOMINAL, BIG, RAPID };
static const char *const boost_names[] = { "nominal", "big", "rapid" };
static enum boost boost = NOMINAL;
static bool boost_extreme;	/* rapid, and still pinned for a second */
static bool led_lit;
static long led_next;		/* when the light next changes; 0: not blinking */
#define LED_WHITE	40	/* of 255, each of the three colours */
#define BOOST_SAMPLE_MS	250
#define BOOST_UP	0.95	/* of one core, by the app in front or the shell */
#define BOOST_DOWN	0.55
#define BIG_SHARE	0.65

/* Which cores ordinary programs may run on: every slice of the system, through
 * the cpuset controller. The kernel's own threads stay where they are. */
static void cores_allowed(const char *cpus) {
	static const char *const groups[] = { "system.slice", "user.slice", "init.scope", "machine.slice" };
	char path[128];
	/* every time: systemd sets the controllers up again as it starts things,
	 * and leaves this one out (it was gone after the boot's first seconds) */
	put("/sys/fs/cgroup/cgroup.subtree_control", "+cpuset");
	for (size_t i = 0; i < sizeof(groups) / sizeof(groups[0]); i++) {
		snprintf(path, sizeof(path), "/sys/fs/cgroup/%s/cpuset.cpus", groups[i]);
		put(path, cpus);
	}
}

/* The big cores but the first, on or off. Only with zrn-hotplug loaded: it is
 * what lets a core come back. */
static const int spare_big[] = { 5, 6, 7 };

static void big_cores(bool on) {
	char path[80], now[8];
	if (access("/sys/module/zrn_hotplug", F_OK) != 0) {
		return;
	}
	for (size_t i = 0; i < sizeof(spare_big) / sizeof(spare_big[0]); i++) {
		snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/online", spare_big[i]);
		FILE *f = fopen(path, "r");
		now[0] = 0;
		if (f) {
			if (!fgets(now, sizeof(now), f)) {
				now[0] = 0;
			}
			fclose(f);
		}
		if (now[0] && (now[0] == '1') != on) {
			put(path, on ? "1" : "0");
		}
	}
}

/* The processor time `pid` has had, in clock ticks, all its threads together. */
static long long cpu_ticks(pid_t pid) {
	char path[64], line[600];
	snprintf(path, sizeof(path), "/proc/%d/stat", pid);
	FILE *f = fopen(path, "r");
	long long ticks = -1;
	if (f && fgets(line, sizeof(line), f)) {
		/* after the name, which may hold anything: field 14 and 15 */
		char *p = strrchr(line, ')');
		unsigned long ut = 0, st = 0;
		if (p && sscanf(p + 2, "%*c %*d %*d %*d %*d %*d %*u %*u %*u %*u %*u %lu %lu", &ut, &st) == 2) {
			ticks = (long long)ut + st;
		}
	}
	if (f) {
		fclose(f);
	}
	return ticks;
}

/* The front light, all three colours together: white, or dark. */
static void led_white(bool on) {
	static const char *const colours[] = { "red", "green", "blue" };
	char path[80];
	for (int i = 0; i < 3; i++) {
		snprintf(path, sizeof(path), "/sys/class/leds/%s:status/brightness", colours[i]);
		put_long(path, on ? LED_WHITE : 0);
	}
	led_lit = on;
}

static void apply(void);
static long boost_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000;
}

/* Whether the boost is working at all now: only in the balanced profile with
 * the screen on. Performance is rapid all the time, power saver never more
 * than big, and with the screen off everything is at its lowest anyway. */
static bool boost_live(void) {
	return screen_on && profile != PERFORMANCE;
}

/* Look at what the app in front and the shell have used since the last look,
 * and step up or down. Returns whether the step changed. */
static bool boost_sample(void) {
	static long last_ms, calm_since;
	static long long last_front = -1, last_shell = -1;
	static pid_t sampled_front;
	long now = boost_ms();
	if (!boost_live()) {
		enum boost want = !screen_on ? NOMINAL : RAPID;
		last_ms = 0;
		if (want == boost) {
			return false;
		}
		boost = want;
		return true;
	}
	long long front = front_pid > 0 ? cpu_ticks(front_pid) : -1;
	long long shell = shell_pid > 0 ? cpu_ticks(shell_pid) : -1;
	double used = 0;
	if (last_ms && now > last_ms) {
		double ticks_per_ms = sysconf(_SC_CLK_TCK) / 1000.0, span = (double)(now - last_ms) * ticks_per_ms;
		if (front >= 0 && last_front >= 0 && sampled_front == front_pid &&
				policy_of(front_pid) != POLICY_ECO) {
			used = (double)(front - last_front) / span;	/* (an eco app's does not count) */
		}
		if (shell >= 0 && last_shell >= 0 && (double)(shell - last_shell) / span > used) {
			used = (double)(shell - last_shell) / span;
		}
	}
	last_ms = now;
	last_front = front;
	last_shell = shell;
	sampled_front = front_pid;

	/* and now and then, that the limit is still in place */
	static int looks;
	if (++looks % 40 == 0 && boost == NOMINAL && access("/sys/fs/cgroup/user.slice/cpuset.cpus", F_OK) != 0) {
		cores_allowed("0-3");
	}
	enum boost was = boost, top = profile == POWERSAVE ? BIG : RAPID;
	static int pinned_looks;
	if (used > BOOST_UP) {
		/* pinned: one step up each look, so rapid comes only if big was not enough */
		calm_since = 0;
		if (boost < top) {
			boost++;
			pinned_looks = 0;
		} else if (boost == RAPID && ++pinned_looks >= 1000 / BOOST_SAMPLE_MS) {
			boost_extreme = true;	/* flat out for a second and still not enough */
		}
	} else {
		pinned_looks = 0;
		boost_extreme = false;
	}
	if (used > BOOST_UP) {
	} else if (used < BOOST_DOWN) {
		/* easy for a while: one step down (two seconds, then three more) */
		if (!calm_since) {
			calm_since = now;
		} else if (boost > NOMINAL && now - calm_since > (boost == RAPID ? 2000 : 3000)) {
			boost--;
			calm_since = now;
		}
	} else {
		calm_since = 0;
	}
	if (boost < BIG && top >= BIG && policy_of(front_pid) == POLICY_BIG) {
		boost = BIG;		/* it asked for the big cores while it is in front */
	}
	return boost != was;
}

static void apply(void) {
	bool low = !screen_on && deepsleep;
	char path[160];
	long table[32];
	for (int c = 0; c < 2; c++) {
		snprintf(path, sizeof(path), CPUFREQ "/policy%d/scaling_available_frequencies", clusters[c]);
		int n = points(path, table, 32);
		if (n == 0) {
			continue;
		}
		long max = low ? table[0] : profile == POWERSAVE ? capped(table, n, 0.65) : table[n - 1];
		bool flat_out = (profile == PERFORMANCE || boost == RAPID) && !low;
		if (!low && c == 1 && boost == NOMINAL) {
			max = table[0];		/* the big cluster: nothing runs there, and slowly */
		} else if (!low && c == 1 && boost == BIG) {
			max = capped(table, n, BIG_SHARE);
		}
		snprintf(path, sizeof(path), CPUFREQ "/policy%d/scaling_governor", clusters[c]);
		put(path, flat_out ? "performance" : "schedutil");
		snprintf(path, sizeof(path), CPUFREQ "/policy%d/scaling_max_freq", clusters[c]);
		put_long(path, max);
	}
	/* Every change of frequency stalls this SoC for about 3 ms while the PLL
	 * relocks, so the governor is made to change its mind less often. */
	put_long(CPUFREQ "/schedutil/rate_limit_us", profile == POWERSAVE ? 40000 : 20000);
	int n = points(GPU "/available_frequencies", table, 32);
	if (n > 0) {
		long top = table[n - 1];
		long max = low ? table[0] : profile == POWERSAVE ? capped(table, n, 0.8) : top;
		put(GPU "/governor", "simple_ondemand");
		/* lower the floor before the ceiling, and raise them the other way */
		put_long(GPU "/min_freq", table[0]);
		put_long(GPU "/max_freq", max);
		if (profile == PERFORMANCE && !low) {
			put_long(GPU "/min_freq", top);
		}
	}
	/* Programs on the efficiency cores only, and the spare big cores off,
	 * until something needs more; coming up, the cores first and then the
	 * leave to use them. */
	bool few = boost == NOMINAL || low;
	if (!few) {
		big_cores(true);
	}
	cores_allowed(few ? "0-3" : "0-7");
	if (few) {
		big_cores(false);
	}
	/* the light: blinking while rapid boost is on (the loop does the blinking) */
	bool blink = boost == RAPID && !low && profile != PERFORMANCE;
	if (blink && !led_next) {
		put("/sys/class/leds/red:status/trigger", "none");
		put("/sys/class/leds/green:status/trigger", "none");
		put("/sys/class/leds/blue:status/trigger", "none");
		led_next = boost_ms();
	} else if (!blink && led_next) {
		led_next = 0;
		led_white(false);
		/* back to red for charging, green for full, or nothing */
		if (system("/usr/local/sbin/zrn-charge >/dev/null 2>&1 &") != 0) {
		}
	}
	fprintf(stderr, "zrnperfd: %s, boost %s, deep sleep %s, screen %s\n", names[profile],
		boost_names[boost], deepsleep ? "on" : "off", screen_on ? "on" : "off");
}

/* the tickle mode for the condition the phone is in, and what is set, for anyone to read */
static void status(void) {
	tickle_set_mode(tickle_when[condition()]);
	FILE *f = fopen(STATUS ".new", "w");
	if (f) {
		fprintf(f, "profile=%s\ndeepsleep=%s\nscreen=%s\n", names[profile],
			deepsleep ? "on" : "off", screen_on ? "on" : "off");
		fprintf(f, "condition=%s\nboost=%s\n", conditions[condition()], boost_names[boost]);
		for (int c = 0; c < 3; c++) {
			fprintf(f, "tickle_%s=%s\n", conditions[c], tickle_names[tickle_when[c]]);
		}
		tickle_status(f);
		fclose(f);
		chmod(STATUS ".new", 0644);
		rename(STATUS ".new", STATUS);
	}
}

static void save(void) {
	mkdir("/var/lib/zrnperfd", 0755);
	FILE *f = fopen(STATE, "w");
	if (f) {
		fprintf(f, "profile %s\ndeepsleep %s\n", names[profile], deepsleep ? "on" : "off");
		for (int c = 0; c < 3; c++) {
			fprintf(f, "tickle %s %s\n", conditions[c], tickle_names[tickle_when[c]]);
		}
		fclose(f);
	}
}

/* 0: nothing changed, 1: the status, 2: the limits too */
static int command(char *line, bool persist) {
	char what[32] = "", value[32] = "", extra[64] = "";
	if (sscanf(line, "%31s %31s %63s", what, value, extra) < 2) {
		return 0;
	}
	if (strcmp(what, "focus") == 0) {
		pid_t pid = atoi(value);
		if (front_pid > 0 && front_pid != pid && front_pid != shell_pid) {
			prioritise(front_pid, 0, 0);	/* back among the rest */
		}
		front_pid = pid;
		if (pid > 0 && pid != shell_pid) {
			prioritise(pid, FRONT_NICE, UI_UCLAMP);
		}
		tickle_focus(pid, extra[0] ? extra : "?");
		return 1;
	}
	if (strcmp(what, "policy") == 0) {
		pid_t pid = atoi(value);
		enum policy mode = strcmp(extra, "eco") == 0 ? POLICY_ECO : strcmp(extra, "big") == 0 ? POLICY_BIG :
			POLICY_NORMAL;
		if (pid > 0) {
			policy_set(pid, mode);
			fprintf(stderr, "zrnperfd: %d asks for %s\n", pid, extra[0] ? extra : "normal");
		}
		return 2;
	}
	if (strcmp(what, "shell") == 0) {
		shell_pid = atoi(value);
		if (shell_pid > 0) {
			prioritise(shell_pid, SHELL_NICE, UI_UCLAMP);
			fprintf(stderr, "zrnperfd: shell %d first in line\n", shell_pid);
		}
		return 0;
	}
	if (strcmp(what, "tickle") == 0) {
		for (int c = 0; c < 3; c++) {
			for (int m = 0; m < 4; m++) {
				if (strcmp(value, conditions[c]) == 0 && strcmp(extra, tickle_names[m]) == 0) {
					tickle_when[c] = (enum tickle_mode)m;
				}
			}
		}
		if (persist) {
			save();
		}
		return 1;
	}
	if (strcmp(what, "profile") == 0) {
		for (int i = 0; i < 3; i++) {
			if (strcmp(value, names[i]) == 0) {
				profile = (enum profile)i;
			}
		}
	} else if (strcmp(what, "deepsleep") == 0) {
		deepsleep = strcmp(value, "on") == 0;
	} else if (strcmp(what, "screen") == 0) {
		screen_on = strcmp(value, "off") != 0;
		persist = false;
		tickle_screen(screen_on);
		/* the modem daemon asks for the signal less often, */
		int mfd = open("/run/ztoscorenetworking.ctl", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
		if (mfd >= 0) {
			if (dprintf(mfd, "screen %s\n", screen_on ? "on" : "off") < 0) {}
			close(mfd);
		}
		/* the sensor daemon stops listening to the room while the screen is off */
		int fd = open("/run/ztoscsf-sensord.ctl", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd >= 0) {
			if (dprintf(fd, "screen %s\n", screen_on ? "on" : "off") < 0) {}
			close(fd);
		}
	} else {
		return 0;
	}
	if (persist) {
		save();
	}
	return 2;
}

static volatile sig_atomic_t stopping;

static void stop(int sig) {
	stopping = 1;
}

/* the kernel's device events, for chargers coming and going */
static int uevents(void) {
	int fd = socket(AF_NETLINK, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, NETLINK_KOBJECT_UEVENT);
	struct sockaddr_nl addr = { .nl_family = AF_NETLINK, .nl_groups = 1 };
	if (fd >= 0 && bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		close(fd);
		fd = -1;
	}
	return fd;
}

int main(void) {
	char line[160];
	setvbuf(stderr, NULL, _IOLBF, 0);
	signal(SIGTERM, stop);
	signal(SIGINT, stop);
	signal(SIGPIPE, SIG_IGN);
	FILE *f = fopen(STATE, "r");
	while (f && fgets(line, sizeof(line), f)) {
		command(line, false);
	}
	if (f) {
		fclose(f);
	}
	plugged = read_plugged();
	tickle_init();
	apply();
	status();

	unlink(CTL);
	if (mkfifo(CTL, 0666) < 0) {
		perror("zrnperfd: " CTL);
		return 1;
	}
	chmod(CTL, 0666);
	/* read and write: with no writer left a read-only pipe reads as closed */
	int ctl = open(CTL, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	if (ctl < 0) {
		perror("zrnperfd: " CTL);
		return 1;
	}
	int uev = uevents();
	char pending[1024];
	size_t pending_len = 0;
	while (!stopping) {
		struct pollfd fds[4] = { { .fd = ctl, .events = POLLIN }, { .fd = uev, .events = POLLIN } };
		int n = 2 + tickle_pollfds(fds + 2, 2);
		/* while the boost is watching, a look four times a second; asleep
		 * and in the performance profile nothing wakes for it */
		int wait = tickle_timeout();
		if (boost_live() && (wait < 0 || wait > BOOST_SAMPLE_MS)) {
			wait = BOOST_SAMPLE_MS;
		}
		if (led_next) {
			/* the light's next change: every 100 ms, every 50 in the extreme phase */
			long now = boost_ms();
			if (now >= led_next) {
				led_white(!led_lit);
				led_next = now + (boost_extreme ? 50 : 100);
			}
			long left = led_next - now;
			if (wait < 0 || wait > left) {
				wait = left < 1 ? 1 : (int)left;
			}
		}
		if (poll(fds, n, wait) < 0 && errno != EINTR) {
			break;
		}
		int changed = 0;
		/* (the light wakes this loop more often than the load is looked at) */
		static long sampled_at;
		if (!led_next || boost_ms() - sampled_at >= BOOST_SAMPLE_MS) {
			sampled_at = boost_ms();
			if (boost_sample()) {
				changed = 2;
			}
		}
		if (fds[0].revents & POLLIN) {
			ssize_t got = read(ctl, pending + pending_len, sizeof(pending) - 1 - pending_len);
			if (got > 0) {
				pending_len += got;
				char *start = pending, *nl;
				while ((nl = memchr(start, '\n', pending + pending_len - start))) {
					*nl = 0;
					int c = command(start, true);
					changed = c > changed ? c : changed;
					start = nl + 1;
				}
				pending_len -= start - pending;
				memmove(pending, start, pending_len);
				if (pending_len == sizeof(pending) - 1) {
					pending_len = 0;	/* a line too long: drop it */
				}
			}
		}
		if (uev >= 0 && (fds[1].revents & POLLIN)) {
			char msg[4096];
			ssize_t got;
			while ((got = recv(uev, msg, sizeof(msg) - 1, 0)) > 0) {
				msg[got] = 0;
				if (memmem(msg, got, "SUBSYSTEM=power_supply", 22)) {
					bool now = read_plugged();
					if (now != plugged && !changed) {
						changed = 1;
					}
					plugged = now;
				}
			}
		}
		tickle_ready(fds + 2, n - 2);
		if (tickle_run() && !changed) {
			changed = 1;	/* an app paused, resumed, or started playing */
		}
		if (changed == 2) {
			apply();
		}
		if (changed) {
			status();
		}
	}
	tickle_stop();
	led_white(false);
	big_cores(true);	/* nothing is left switched off behind us */
	cores_allowed("0-7");
	fprintf(stderr, "zrnperfd: stopped, every app running\n");
	return 0;
}
