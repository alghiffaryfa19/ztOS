# Builds on the phone itself (aarch64 Arch Linux ARM, wlroots 0.20).
PKGS = wlroots-0.20 wayland-server xkbcommon cairo pangocairo librsvg-2.0 pixman-1 libdrm egl glesv2
APP_PKGS = xkbcommon wayland-client wayland-egl egl glesv2 cairo pangocairo
WP := $(shell pkg-config --variable=pkgdatadir wayland-protocols)
XDG := $(WP)/stable/xdg-shell/xdg-shell.xml
CFLAGS ?= -O2 -g
WARN = -std=c11 -Wall -Wno-unused-parameter

all: auth/ztos-polkit-agent sounds/ztos-sounds ztmetro apps/ztapps settings/ztsettings devtest/zrndevtest perfd/zrnperfd sensord/ztoscsf-sensord modemd/ztoscorenetworkingd \
	csf/ztoscsf notifyd/ztnotifyd phone/ztphone messages/ztmessages contacts/ztcontacts

sounds/ztos-sounds: sounds/ztos-sounds.c
	$(CC) $(CFLAGS) $(WARN) $(shell pkg-config --cflags libpulse) -o $@ $< $(shell pkg-config --libs libpulse) -lm

ztmetro: ztmetro.c auth/prompt.c xdg-shell-protocol.h
	$(CC) $(CFLAGS) $(WARN) -DWLR_USE_UNSTABLE -I. $(shell pkg-config --cflags $(PKGS)) \
		-o $@ ztmetro.c $(shell pkg-config --libs $(PKGS)) -lm

xdg-shell-protocol.h:
	wayland-scanner server-header $(XDG) $@

# the list of applications and their glyphs, kept for the shell
apps/ztapps: apps/ztapps.c
	$(CC) $(CFLAGS) $(WARN) $(shell pkg-config --cflags cairo gdk-pixbuf-2.0 librsvg-2.0) -o $@ apps/ztapps.c \
		$(shell pkg-config --libs cairo gdk-pixbuf-2.0 librsvg-2.0) -lm

# ZrnMetroCore: the window, motion and widgets our own apps share
VIEWPORTER := $(WP)/stable/viewporter/viewporter.xml
CORE = core/zrnmetrocore.c core/zrnmetrogpu.c core/xdg-shell-protocol.c core/viewporter-protocol.c
CORE_DEPS = $(CORE) core/zrnmetrocore.h core/zrnmetrogpu.h core/zrnmetrorender.h core/xdg-shell-client-protocol.h core/viewporter-client-protocol.h
APP_BUILD = $(CC) $(CFLAGS) $(WARN) -Icore $(shell pkg-config --cflags $(APP_PKGS))
APP_LIBS = $(shell pkg-config --libs $(APP_PKGS)) -lm

settings/ztsettings: settings/ztsettings.c $(CORE_DEPS)
	$(APP_BUILD) -o $@ settings/ztsettings.c $(CORE) $(APP_LIBS)

devtest/zrndevtest: devtest/zrndevtest.c $(CORE_DEPS)
	$(APP_BUILD) -o $@ devtest/zrndevtest.c $(CORE) $(APP_LIBS)

perfd/zrnperfd: perfd/zrnperfd.c perfd/tickle.c perfd/tickle.h
	$(CC) $(CFLAGS) $(WARN) -o $@ perfd/zrnperfd.c perfd/tickle.c

sensord/ztoscsf-sensord: sensord/ztoscsf-sensord.c
	$(CC) $(CFLAGS) $(WARN) -o $@ sensord/ztoscsf-sensord.c -lm

# the modem's state for the shell (signal, operator, data), and airplane mode, SMS
modemd/ztoscorenetworkingd: modemd/ztoscorenetworkingd.c
	$(CC) $(CFLAGS) $(WARN) -o $@ modemd/ztoscorenetworkingd.c

# notifications from other programs, what is playing, and push
notifyd/ztnotifyd: notifyd/ztnotifyd.c
	$(CC) $(CFLAGS) $(WARN) $(shell pkg-config --cflags libsystemd) -o $@ notifyd/ztnotifyd.c \
		$(shell pkg-config --libs libsystemd)

# the phone's own three: calls, messages, people (comm.h is what they share)
phone/ztphone: phone/ztphone.c comm/comm.h $(CORE_DEPS)
	$(APP_BUILD) -Icomm -o $@ phone/ztphone.c $(CORE) $(APP_LIBS)

messages/ztmessages: messages/ztmessages.c comm/comm.h $(CORE_DEPS)
	$(APP_BUILD) -Icomm -o $@ messages/ztmessages.c $(CORE) $(APP_LIBS)

contacts/ztcontacts: contacts/ztcontacts.c comm/comm.h $(CORE_DEPS)
	$(APP_BUILD) -Icomm -o $@ contacts/ztcontacts.c $(CORE) $(APP_LIBS)

# ztOSCoreSensorFramework: starts every piece of hardware support with the
# system, watches them, and reports what fails
csf/ztoscsf: csf/ztoscsf.c
	$(CC) $(CFLAGS) $(WARN) -o $@ csf/ztoscsf.c

splash/ztos-splash: splash/ztos-splash.c
	$(CC) $(CFLAGS) $(WARN) $(shell pkg-config --cflags cairo pangocairo) -o $@ splash/ztos-splash.c \
		$(shell pkg-config --libs cairo pangocairo)

core/xdg-shell-client-protocol.h:
	wayland-scanner client-header $(XDG) $@

core/xdg-shell-protocol.c:
	wayland-scanner private-code $(XDG) $@

# wp_viewport: the list's surface shows a part of the page (see zrnmetrocore.c)
core/viewporter-client-protocol.h:
	wayland-scanner client-header $(VIEWPORTER) $@

core/viewporter-protocol.c:
	wayland-scanner private-code $(VIEWPORTER) $@

install: all
	install -Dm755 auth/ztos-polkit-agent /usr/local/bin/ztos-polkit-agent
	install -Dm755 sounds/ztos-sounds /usr/local/bin/ztos-sounds
	install -Dm755 ztmetro /usr/local/bin/ztmetro
	install -Dm755 ztmetro-session /usr/local/bin/ztmetro-session
	install -Dm755 splash/ztos-splash /usr/local/bin/ztos-splash
	install -Dm755 apps/ztapps /usr/local/bin/ztapps
	install -Dm755 settings/ztsettings /usr/local/bin/ztsettings
	install -Dm755 devtest/zrndevtest /usr/local/bin/zrndevtest
	install -Dm755 perfd/zrnperfd /usr/local/sbin/zrnperfd
	install -Dm644 perfd/zrnperfd.service /etc/systemd/system/zrnperfd.service
	install -Dm755 sensord/ztoscsf-sensord /usr/local/sbin/ztoscsf-sensord
	install -Dm644 sensord/ztoscsf-sensord.service /etc/systemd/system/ztoscsf-sensord.service
	install -Dm755 modemd/ztoscorenetworkingd /usr/local/sbin/ztoscorenetworkingd
	install -Dm644 modemd/ztoscorenetworking-modem.service /etc/systemd/system/ztoscorenetworking-modem.service
	install -Dm644 modemd/ztoscorenetworking.service /etc/systemd/system/ztoscorenetworking.service
	install -Dm755 csf/ztoscsf /usr/local/sbin/ztoscsf
	install -Dm755 notifyd/ztnotifyd /usr/local/bin/ztnotifyd
	install -Dm755 phone/ztphone /usr/local/bin/ztphone
	install -Dm755 messages/ztmessages /usr/local/bin/ztmessages
	install -Dm755 contacts/ztcontacts /usr/local/bin/ztcontacts
	install -Dm644 -t /usr/local/share/applications comm/ztphone.desktop comm/ztmessages.desktop comm/ztcontacts.desktop
	install -Dm644 -t /usr/local/share/icons/hicolor/scalable/apps comm/ztphone.svg comm/ztmessages.svg comm/ztcontacts.svg
	install -Dm644 csf/components /etc/ztoscoresensorframework/components
	install -Dm644 csf/ztoscoresensorframework.service /etc/systemd/system/ztoscoresensorframework.service

clean:
	rm -f auth/ztos-polkit-agent sounds/ztos-sounds ztmetro apps/ztapps xdg-shell-protocol.h settings/ztsettings devtest/zrndevtest perfd/zrnperfd sensord/ztoscsf-sensord modemd/ztoscorenetworkingd csf/ztoscsf \
		core/xdg-shell-client-protocol.h core/xdg-shell-protocol.c

# Uses a private Wayland seat; does not touch the running compositor or display.
.PHONY: check-keyboard
check-keyboard: xdg-shell-protocol.h
	$(CC) -O0 -g $(WARN) -DWLR_USE_UNSTABLE -I. $(shell pkg-config --cflags $(PKGS)) \
		-o /tmp/ztmetro-keyboard-test tests/keyboard-trackpad.c $(shell pkg-config --libs $(PKGS)) -lm
	/tmp/ztmetro-keyboard-test

.PHONY: check-launch
check-launch: xdg-shell-protocol.h
	$(CC) -O0 -g $(WARN) -DWLR_USE_UNSTABLE -I. $(shell pkg-config --cflags $(PKGS)) \
		-o /tmp/ztmetro-launch-test tests/launch-state.c $(shell pkg-config --libs $(PKGS)) -lm
	/tmp/ztmetro-launch-test

.PHONY: check-auth
check-auth: xdg-shell-protocol.h
	$(CC) -O0 -g $(WARN) -DWLR_USE_UNSTABLE -I. $(shell pkg-config --cflags $(PKGS)) \
		-o /tmp/ztmetro-auth-test tests/auth-prompt.c $(shell pkg-config --libs $(PKGS)) -lm
	/tmp/ztmetro-auth-test

# The native agent uses polkit's async callback API directly.
auth/ztos-polkit-agent: auth/ztos-polkit-agent.c
	$(CC) $(CFLAGS) $(WARN) $(shell pkg-config --cflags polkit-agent-1 gio-unix-2.0) -o $@ $< $(shell pkg-config --libs polkit-agent-1 gio-unix-2.0)
