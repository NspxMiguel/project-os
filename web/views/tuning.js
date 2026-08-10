// views/tuning.js — Ajustes de hardware: fan, clock, LEDs, HDMI, Wi-Fi, GPU.
//
// *"ai opção de diminuir ventoinha do rp, e etc. varias coisas legais adiciona"*
//
// `project_os/core/tuning.py` splits every knob into two kinds of change, and
// this screen keeps that split visible instead of hiding it behind one button:
//
//   NOW      — a /sys write. Immediate, and gone the moment the board reboots.
//   ON BOOT  — a config.txt line. Needs a reboot, survives every one after.
//
// A board that does not expose a knob (a Pi 4's HAT fan wired straight to 5V,
// a non-Pi dev machine with no config.txt) says so in `available`/`reason`
// rather than the API guessing — this screen repeats that plainly instead of
// drawing a slider that would just 404.

import {h, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {setStrings, t} from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';

setStrings('en', {
  'tuning.title': 'Hardware tuning',
  'tuning.lead': 'The knobs on the board itself. Changes marked "on boot" are written to config.txt and need a reboot to take effect.',
  'tuning.error.load': 'Could not read the hardware tuning screen.',
  'tuning.disabled.title': 'Hardware control is off',
  'tuning.disabled.text': 'Turn on security.allow_hardware_control in Settings > Developer to change the fan, clock or LEDs from here. Reading still works without it.',
  'tuning.notRoot.title': 'Read-only from here',
  'tuning.notRoot.text': 'project-os is not running as root, so these values can be read but not changed. The systemd service runs as root; a venv opened by hand in a terminal does not.',

  'tuning.section.fan': 'Fan',
  'tuning.fan.unavailable': 'No fan the kernel can control',
  'tuning.fan.level': 'Current level',
  'tuning.fan.setLevel': 'Set level now',
  'tuning.fan.levelNote': 'Temporary — the firmware’s own curve overrides this the moment the temperature changes. To make the fan quieter for good, raise the thresholds below.',
  'tuning.fan.curve': 'Spin-up thresholds (on boot)',
  'tuning.fan.curve.step': 'Step {n}',
  'tuning.fan.curve.celsius': '°C',
  'tuning.fan.curve.speed': 'speed',
  'tuning.fan.curve.save': 'Save curve',
  'tuning.fan.levelSet': 'Fan set to {level}/{max}',
  'tuning.fan.curveSaved': 'Fan curve saved — takes effect on the next boot',

  'tuning.section.clock': 'CPU clock',
  'tuning.clock.unavailable': 'This machine does not expose cpufreq',
  'tuning.clock.current': 'Current',
  'tuning.clock.range': 'Range',
  'tuning.clock.governor': 'Governor',
  'tuning.clock.governorSet': 'Governor set to {name}',
  'tuning.clock.maxClock': 'Boot clock cap (on boot)',
  'tuning.clock.maxClock.hint': 'Leave blank for the board’s stock speed. No overclock helper here on purpose — raising this past stock also wants more voltage and a real heatsink, decisions a slider should not make by accident.',
  'tuning.clock.maxClock.save': 'Save cap',
  'tuning.clock.maxClock.saved': 'Clock cap saved — takes effect on the next boot',
  'tuning.clock.maxClock.cleared': 'Clock cap removed — back to stock on the next boot',

  'tuning.section.leds': 'LEDs',
  'tuning.leds.empty': 'No controllable LEDs found',
  'tuning.leds.on': 'On',
  'tuning.leds.off': 'Off',
  'tuning.leds.trigger': 'Trigger',
  'tuning.leds.updated': '{name} updated',

  'tuning.section.hdmi': 'HDMI output',
  'tuning.hdmi.unavailable': 'vcgencmd is not available on this machine',
  'tuning.hdmi.on': 'Output on',
  'tuning.hdmi.off': 'Output off',
  'tuning.hdmi.updated': 'HDMI output {state}',

  'tuning.section.wifi': 'Wi-Fi power save',
  'tuning.wifi.unavailable': 'No Wi-Fi interface found',
  'tuning.wifi.hint': 'Anything that expects to be reached — this very page included — wants power save off. A sensor that only ever sends wants it on.',
  'tuning.wifi.updated': '{interface} power save {state}',

  'tuning.section.gpu': 'GPU memory split',
  'tuning.gpu.current': 'Currently',
  'tuning.gpu.boot': 'On boot',
  'tuning.gpu.note': 'On a headless box, 16 MB is enough and gives the rest back to apps.',
  'tuning.gpu.save': 'Save split',
  'tuning.gpu.saved': 'GPU memory split saved — takes effect on the next boot',

  'tuning.reboot.title': 'This needs a reboot',
  'tuning.reboot.detail': 'This is written to config.txt and only takes effect after the board restarts — it will not change anything right now.',
  'tuning.temporary.badge': 'now, temporary',
  'tuning.reboot.badge': 'on boot',
  'tuning.notAvailableShort': 'Not available on this hardware',
}, {activate: false});

/* --------------------------------------------------------------- helpers */

function card(title, badge, body) {
  return h('div', {class: 'card'},
    h('div', {class: 'card__header'},
      h('div', {class: 'grow'}, h('div', {class: 'card__title'}, title)),
      badge ? h('div', {class: 'card__tools'}, badge) : null,
    ),
    h('div', {class: 'card__body'}, body),
  );
}

function skeletonCard() {
  return h('div', {class: 'card'},
    h('div', {class: 'card__header'}, h('div', {class: 'skeleton skeleton--title', style: {flex: '1'}})),
    h('div', {class: 'card__body'},
      h('div', {class: 'skeleton skeleton--text'}),
      h('div', {class: 'skeleton skeleton--block'}),
    ),
  );
}

function errorState(message, onRetry) {
  return h('div', {class: 'notice notice--error'},
    icon('warning', {size: 18}),
    h('div', {class: 'notice__body'},
      h('span', null, message),
      onRetry
        ? h('div', {class: 'notice__actions'},
            h('button', {class: 'btn btn--sm', type: 'button', onClick: onRetry},
              icon('refresh', {size: 14}), t('action.retry')),
          )
        : null,
    ),
  );
}

function unavailableNote(reason) {
  return h('div', {class: 'empty empty--sm'},
    h('span', {class: 'empty__icon'}, icon('info', {size: 18})),
    h('p', {class: 'empty__text'}, reason || t('tuning.notAvailableShort')),
  );
}

function bootBadge() {
  return h('span', {class: 'badge badge--warn'}, icon('clock', {size: 12}), t('tuning.reboot.badge'));
}

function nowBadge() {
  return h('span', {class: 'badge badge--plain'}, t('tuning.temporary.badge'));
}

/** Every write that touches config.txt asks first — the backend cannot undo a
 *  board that will not boot, so the warning has to land before the click. */
async function confirmReboot() {
  return confirm(t('tuning.reboot.title'), {detail: t('tuning.reboot.detail'), confirmLabel: t('action.continue')});
}

/* ------------------------------------------------------------------ view */

export default {
  id: 'tuning',
  title: 'Ajustes de hardware',

  async mount(root, ctx) {
    let disposed = false;
    let snapshot = null;
    let loadError = null;

    // Local edit buffers so a table of inputs does not fight the user by
    // re-rendering mid-keystroke; they are seeded from the snapshot on load.
    let fanSteps = [];
    let maxClockInput = '';
    let gpuInput = '';

    const body = h('div', {class: 'stack stack--lg'});
    root.appendChild(h('div', {class: 'stack stack--lg'},
      h('div', {class: 'page__header'},
        h('h1', null, t('tuning.title')),
        h('p', {class: 'page__lead'}, t('tuning.lead')),
      ),
      body,
    ));

    function paintLoading() {
      clear(body);
      body.appendChild(skeletonCard());
      body.appendChild(skeletonCard());
      body.appendChild(skeletonCard());
    }

    function paintErrorOnly() {
      clear(body);
      body.appendChild(errorState(loadError, load));
    }

    /* -------------------------------------------------------- gate notice */

    function gateNotice() {
      if (!snapshot) return null;
      if (!snapshot.enabled) {
        return h('div', {class: 'notice notice--warn'},
          icon('lock', {size: 18}),
          h('div', {class: 'notice__body'},
            h('div', {class: 'notice__title'}, t('tuning.disabled.title')),
            h('p', null, snapshot.reason || t('tuning.disabled.text')),
          ),
        );
      }
      if (!snapshot.writable) {
        return h('div', {class: 'notice notice--info'},
          icon('info', {size: 18}),
          h('div', {class: 'notice__body'},
            h('div', {class: 'notice__title'}, t('tuning.notRoot.title')),
            h('p', null, snapshot.reason || t('tuning.notRoot.text')),
          ),
        );
      }
      return null;
    }

    /** Every write button is disabled unless the snapshot says a write can
     *  actually succeed — the same gate the API enforces, shown before the
     *  click instead of after a 403. */
    function canWrite() {
      return !!(snapshot && snapshot.enabled && snapshot.writable);
    }

    async function apply(action, successMessage) {
      try {
        await action();
        toast(successMessage, {type: 'success'});
        await load();
      } catch (err) {
        toast((err && err.message) || t('tuning.error.load'), {type: 'error'});
      }
    }

    /* --------------------------------------------------------------- fan */

    function fanCard() {
      const fan = snapshot.fan || {};
      if (!fan.available) {
        return card(t('tuning.section.fan'), null, unavailableNote(fan.reason));
      }
      const device = (fan.devices || [])[0] || {};
      const levelRow = h('div', {class: 'stack stack--sm'},
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('tuning.fan.level')),
          h('span', {class: 'field-row__value'}, device.level + ' / ' + device.max_level),
        ),
        h('input', {
          type: 'range', min: 0, max: device.max_level, value: device.level,
          disabled: !canWrite(),
          onChange: (event) => setFanLevel(device.name, Number(event.target.value)),
        }),
        h('p', {class: 'small muted'}, t('tuning.fan.levelNote')),
      );

      const curveRows = fanSteps.map((step, index) => h('div', {class: 'row'},
        h('span', {class: 'small muted'}, t('tuning.fan.curve.step', {n: step.step})),
        h('input', {
          type: 'number', min: 30, max: 85, value: step.celsius, style: {width: '5em'},
          disabled: !canWrite(),
          onChange: (event) => { fanSteps[index] = {...step, celsius: Number(event.target.value)}; },
        }),
        h('span', {class: 'small muted'}, t('tuning.fan.curve.celsius')),
        h('input', {
          type: 'number', min: 0, max: 255, value: step.speed, style: {width: '5em'},
          disabled: !canWrite(),
          onChange: (event) => { fanSteps[index] = {...step, speed: Number(event.target.value)}; },
        }),
        h('span', {class: 'small muted'}, t('tuning.fan.curve.speed')),
      ));

      const curve = h('div', {class: 'stack stack--sm'},
        h('div', {class: 'row row--between'},
          h('span', {class: 'field__label'}, t('tuning.fan.curve')),
          bootBadge(),
        ),
        h('div', {class: 'stack stack--sm'}, curveRows),
        h('button', {
          class: 'btn btn--sm', type: 'button', disabled: !canWrite(),
          onClick: saveFanCurve,
        }, t('tuning.fan.curve.save')),
      );

      return card(t('tuning.section.fan'), nowBadge(), h('div', {class: 'stack'}, levelRow, curve));
    }

    async function setFanLevel(deviceName, level) {
      await apply(
        () => api.post('/tuning/fan/level', {level, device: deviceName}),
        t('tuning.fan.levelSet', {level, max: (snapshot.fan.devices[0] || {}).max_level}),
      );
    }

    async function saveFanCurve() {
      const ok = await confirmReboot();
      if (!ok) return;
      await apply(
        () => api.post('/tuning/fan/curve', {steps: fanSteps}),
        t('tuning.fan.curveSaved'),
      );
    }

    /* ------------------------------------------------------------- clock */

    function clockCard() {
      const clock = snapshot.clock || {};
      if (!clock.available) {
        return card(t('tuning.section.clock'), null, unavailableNote(t('tuning.clock.unavailable')));
      }
      const governorSelect = h('select', {
        value: clock.governor || '', disabled: !canWrite(),
        onChange: (event) => setGovernor(event.target.value),
      }, (clock.governors || []).map((g) => h('option', {value: g}, g)));

      const now = h('div', {class: 'fields'},
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('tuning.clock.current')),
          h('span', {class: 'field-row__value'}, clock.current_mhz ? clock.current_mhz + ' MHz' : '—'),
        ),
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('tuning.clock.range')),
          h('span', {class: 'field-row__value'},
            (clock.min_mhz && clock.max_mhz) ? clock.min_mhz + '–' + clock.max_mhz + ' MHz' : '—'),
        ),
        h('div', {class: 'field'},
          h('label', {class: 'field__label'}, t('tuning.clock.governor')),
          governorSelect,
        ),
      );

      const cap = h('div', {class: 'stack stack--sm'},
        h('div', {class: 'row row--between'},
          h('span', {class: 'field__label'}, t('tuning.clock.maxClock')),
          bootBadge(),
        ),
        h('p', {class: 'small muted'}, t('tuning.clock.maxClock.hint')),
        h('div', {class: 'row'},
          h('input', {
            type: 'number', min: 600, max: 3000, placeholder: clock.boot_arm_freq || '',
            value: maxClockInput, disabled: !canWrite(), style: {width: '8em'},
            onInput: (event) => { maxClockInput = event.target.value; },
          }),
          h('span', {class: 'small muted'}, 'MHz'),
          h('button', {
            class: 'btn btn--sm', type: 'button', disabled: !canWrite(),
            onClick: saveMaxClock,
          }, t('tuning.clock.maxClock.save')),
        ),
      );

      return card(t('tuning.section.clock'), nowBadge(), h('div', {class: 'stack'}, now, cap));
    }

    async function setGovernor(name) {
      await apply(
        () => api.post('/tuning/clock/governor', {governor: name}),
        t('tuning.clock.governorSet', {name}),
      );
    }

    async function saveMaxClock() {
      const ok = await confirmReboot();
      if (!ok) return;
      const mhz = maxClockInput.trim() === '' ? null : Number(maxClockInput);
      await apply(
        () => api.post('/tuning/clock/max', {mhz}),
        mhz === null ? t('tuning.clock.maxClock.cleared') : t('tuning.clock.maxClock.saved'),
      );
    }

    /* -------------------------------------------------------------- leds */

    function ledsCard() {
      const leds = snapshot.leds || [];
      if (!leds.length) {
        return card(t('tuning.section.leds'), null, unavailableNote(t('tuning.leds.empty')));
      }
      const rows = leds.map((led) => h('div', {class: 'list__row'},
        h('div', {class: 'list__main'},
          h('div', {class: 'list__title'}, led.name),
          led.trigger ? h('div', {class: 'list__sub'}, t('tuning.leds.trigger') + ': ' + led.trigger) : null,
        ),
        h('div', {class: 'list__aside'},
          h('label', {class: 'switch'},
            h('input', {
              type: 'checkbox', checked: led.on, disabled: !canWrite(),
              onChange: (event) => setLed(led.name, event.target.checked),
            }),
            h('span', {class: 'switch__track'}),
            led.on ? t('tuning.leds.on') : t('tuning.leds.off'),
          ),
        ),
      ));
      return card(t('tuning.section.leds'), nowBadge(), h('div', {class: 'list'}, rows));
    }

    async function setLed(name, on) {
      await apply(
        () => api.post('/tuning/leds/' + encodeURIComponent(name), {on}),
        t('tuning.leds.updated', {name}),
      );
    }

    /* -------------------------------------------------------------- hdmi */

    function hdmiCard() {
      const hdmi = snapshot.hdmi || {};
      if (!hdmi.available) {
        return card(t('tuning.section.hdmi'), null, unavailableNote(t('tuning.hdmi.unavailable')));
      }
      const row = h('div', {class: 'row row--between'},
        h('span', null, hdmi.on ? t('tuning.hdmi.on') : t('tuning.hdmi.off')),
        h('label', {class: 'switch'},
          h('input', {
            type: 'checkbox', checked: !!hdmi.on, disabled: !canWrite(),
            onChange: (event) => setHdmi(event.target.checked),
          }),
          h('span', {class: 'switch__track'}),
        ),
      );
      return card(t('tuning.section.hdmi'), nowBadge(), row);
    }

    async function setHdmi(on) {
      await apply(
        () => api.post('/tuning/hdmi', {on}),
        t('tuning.hdmi.updated', {state: on ? t('tuning.hdmi.on') : t('tuning.hdmi.off')}),
      );
    }

    /* -------------------------------------------------------------- wifi */

    function wifiCard() {
      const wifi = snapshot.wifi || {};
      if (!wifi.available || !(wifi.interfaces || []).length) {
        return card(t('tuning.section.wifi'), null, unavailableNote(wifi.reason || t('tuning.wifi.unavailable')));
      }
      const rows = wifi.interfaces.map((iface) => h('div', {class: 'row row--between'},
        h('span', null, iface.interface),
        h('label', {class: 'switch'},
          h('input', {
            type: 'checkbox', checked: !!iface.power_save, disabled: !canWrite(),
            onChange: (event) => setWifiPowerSave(iface.interface, event.target.checked),
          }),
          h('span', {class: 'switch__track'}),
        ),
      ));
      return card(t('tuning.section.wifi'), nowBadge(),
        h('div', {class: 'stack stack--sm'},
          h('p', {class: 'small muted'}, t('tuning.wifi.hint')),
          h('div', {class: 'stack stack--sm'}, rows),
        ));
    }

    async function setWifiPowerSave(iface, on) {
      await apply(
        () => api.post('/tuning/wifi/power-save', {on, interface: iface}),
        t('tuning.wifi.updated', {interface: iface, state: on ? t('tuning.leds.on') : t('tuning.leds.off')}),
      );
    }

    /* --------------------------------------------------------------- gpu */

    function gpuCard() {
      const gpu = snapshot.gpu || {};
      const range = gpu.range_mb || [16, 512];
      const now = h('div', {class: 'fields'},
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('tuning.gpu.current')),
          h('span', {class: 'field-row__value'}, gpu.current_mb !== null && gpu.current_mb !== undefined ? gpu.current_mb + ' MB' : '—'),
        ),
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('tuning.gpu.boot')),
          h('span', {class: 'field-row__value'}, gpu.boot_value || '—'),
        ),
      );
      const editor = h('div', {class: 'stack stack--sm'},
        h('div', {class: 'row row--between'}, h('span', {class: 'field__label'}, ''), bootBadge()),
        h('p', {class: 'small muted'}, t('tuning.gpu.note')),
        h('div', {class: 'row'},
          h('input', {
            type: 'number', min: range[0], max: range[1], placeholder: gpu.boot_value || '',
            value: gpuInput, disabled: !canWrite(), style: {width: '8em'},
            onInput: (event) => { gpuInput = event.target.value; },
          }),
          h('span', {class: 'small muted'}, 'MB'),
          h('button', {
            class: 'btn btn--sm', type: 'button', disabled: !canWrite() || !gpuInput,
            onClick: saveGpuMemory,
          }, t('tuning.gpu.save')),
        ),
      );
      return card(t('tuning.section.gpu'), nowBadge(), h('div', {class: 'stack'}, now, editor));
    }

    async function saveGpuMemory() {
      const ok = await confirmReboot();
      if (!ok) return;
      await apply(
        () => api.post('/tuning/gpu-memory', {mb: Number(gpuInput)}),
        t('tuning.gpu.saved'),
      );
    }

    /* ------------------------------------------------------------ layout */

    function paintAll() {
      if (disposed) return;
      clear(body);
      const notice = gateNotice();
      if (notice) body.appendChild(notice);
      body.appendChild(fanCard());
      body.appendChild(clockCard());
      body.appendChild(ledsCard());
      body.appendChild(hdmiCard());
      body.appendChild(wifiCard());
      body.appendChild(gpuCard());
    }

    async function load() {
      loadError = null;
      paintLoading();
      try {
        snapshot = await api.get('/tuning');
        fanSteps = ((snapshot.fan || {}).thresholds || []).map((s) => ({...s}));
        maxClockInput = '';
        gpuInput = '';
        paintAll();
      } catch (err) {
        loadError = (err && err.message) || t('tuning.error.load');
        paintErrorOnly();
      }
    }

    await load();

    return () => {
      disposed = true;
    };
  },
};
