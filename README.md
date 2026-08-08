# Gree GMV Cloud for Home Assistant

[简体中文](README.zh-CN.md) | English

An unofficial Home Assistant custom integration for a Gree GMV central air-conditioning project exposed through the WeChat mini-program **Gree Central Air Conditioner Assistant** (格力中央空调微助手).

This integration was developed for one owner-operated installation with a `GMV-H200WL/H2S` outdoor unit, `XC71-33/H2` wired controllers, and an existing cellular DTU. It does **not** use the LAN protocol implemented by the standard Home Assistant Gree integration.

## Status and safety boundary

- Cloud polling through Gree's private, undocumented API.
- One climate entity per indoor unit returned by the owner's project.
- No SMS automation, WeChat protocol emulation, account binding, or access to equipment owned by anyone else.
- No local-control claim: operation depends on Gree's cloud service and the installed cellular DTU.
- Tested with Home Assistant Container 2026.7.1 and one five-zone installation.
- Pre-release software. Use only while someone is present until each room has been calibrated.

The integration and its authors are not affiliated with or endorsed by Gree Electric Appliances Inc.

## Features

- Per-room power control.
- Cooling, heating, dry, fan-only, and master-only auto mode.
- Half-degree target temperatures from 16–30 °C.
- Automatic fan target by default, plus five explicit per-room targets.
- Every real room power-on transition starts with automatic fan.
- Current room/controller temperature when supplied by the cloud.
- Online, configured mode, reported fan level, and error-presence attributes.
- Automatic Bearer-token refresh six hours before JWT expiry.
- Home Assistant reauthentication when the cloud rejects a credential.
- Automatic master-unit discovery from the cloud's `mainIDU` field.
- Master/slave mode filtering against the visible cooling/heating direction.

The API reports configured mode and power but no verified compressor-demand bit. The integration therefore does not invent an active cooling/heating `hvac_action`.

## Master/slave direction behavior

The tested GMV project has one master wired controller. Only that master can select auto or change the system between cooling and heating. A slave never exposes auto. In cooling direction, slaves offer cooling, dry, and fan-only; in heating direction, they offer heating and fan-only.

Master auto still activates a real cooling or heating direction: both the auto lamp and the corresponding direction lamp illuminate on the wired controller. However, the captured `getUnits` response contains only `mode=5` and no second field for that lamp. When an active slave reports a directional mode, the integration uses it as the visible direction. If there is no such evidence, both cooling-side and heating-side slave choices remain visible and one command is submitted; the GMV controller is allowed to accept or reject it. A definite cloud rejection is surfaced to Home Assistant and the state is refreshed immediately. Writes are never retried after transport ambiguity.

## Installation with HACS

1. In HACS, open the top-right menu and choose **Custom repositories**.
2. Add `https://github.com/bitcinnamon/gree-gmv-cloud-ha` as type **Integration**.
3. Download **Gree GMV Cloud**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration → Gree GMV Cloud**.

HACS only supports public GitHub repositories. This repository deliberately contains no account credentials, captures, device identifiers, room names, or original mini-program files.

## Credentials

Initial setup requires three values from the owner's already-authenticated mini-program session:

- `Authorization` Bearer token, including or excluding the `Bearer ` prefix;
- WeChat `openId`;
- Gree `uid`.

An ordinary authenticated request to

```text
POST https://a.gree.com:7016/gree2/app/v2.0/control/getUnits
```

contains the Bearer token in its `Authorization` request header and `openId`/`uid` in its form body. Obtain these only from your own session. Do not upload a HAR file, paste credentials into an issue, or commit Home Assistant's `.storage` directory.

The observed JWT lifetime is 72 hours. While Home Assistant is running, the integration refreshes it before expiry and immediately persists the replacement. If that refresh chain breaks, Home Assistant asks only for a new token; the existing `openId` and `uid` remain configured.

Home Assistant stores Config Entry data in its configuration directory. Protect that directory and its backups; it is not a dedicated encrypted secrets vault.

## Fan-target policy

The mini-program's current protocol writes a complete target state. Its status and command fan-speed values are offset, so an automatic target cannot be inferred from the reported execution level. The integration uses automatic fan as the safe installation default for every room that has no explicit Home Assistant selection.

An explicit per-room fan selection overrides that default and is persisted in the Home Assistant config entry across restarts and upgrades. When a room is off, in auto, or in dry mode, changing the target only updates Home Assistant and does not send a cloud control request. When a room is already running in cooling, heating, or fan-only mode, selecting a fan target submits a normal full-state write and should be verified at the wired controller.

Before every write, the integration reads fresh cloud state. If that state shows the room is off and the requested operation turns it on, the complete control payload always uses automatic fan (`windSpeed=1`) and the saved Home Assistant target is updated to automatic. An idempotent power-on request for an already-running room does not reset its fan target.

While a fixed target is cached, a different fixed execution level reported by an active cooling, heating, or fan-only unit is treated as a wired-controller change. The integration adopts that level during polling and rechecks it immediately before any full-state write, so changing temperature does not restore a stale fixed fan target. An automatic target is never inferred from execution level and is always preserved as automatic.

For HomeKit's linked climate fan, the integration exposes automatic as a separate Auto/Manual choice plus the [four standard fixed names that Home Assistant recognizes](https://github.com/home-assistant/core/blob/7c5eb60b7a98a3900cece9d8719f7a5620f459e7/homeassistant/components/homekit/climate_util.py#L27-L55). The fixed slider renders `25/50/75/100%` as levels `1/2/3/5`. HomeKit reserves 0% for an inactive fan, and Home Assistant [discards a bare zero-speed write](https://github.com/home-assistant/core/blob/7c5eb60b7a98a3900cece9d8719f7a5620f459e7/homeassistant/components/homekit/climate_base.py#L219-L229), so 0% is not a fifth writable speed. Selecting Auto, or using the linked fan's off action when HomeKit supplies it, sends automatic (`windSpeed=1`); the main climate power switch remains the only way to turn the room off. If the room is already off, selecting Auto only saves it as the next target and never turns the room on. Automatic has no server target field, so HomeKit may retain the last fixed slider position while its Auto choice is selected. Fixed-level display comes from the latest server report rather than the requested target: it is first checked three seconds after a write and, if needed, once more at five seconds total before rolling back. Physical level 4 remains visible in `reported_fan_level` but is rendered at the nearer 75% HomeKit step.

Dry and auto modes follow the current mini-program UI restrictions and do not allow fan adjustment. Temperature changes are blocked while the room is off or in auto mode. Mode changes may restore that mode's remembered setpoint, so the integration always polls the actual state after a write.

## Known limitations

- Private cloud endpoints may change or be withdrawn without notice.
- Initial credential acquisition is manual; the integration cannot mint a WeChat `jsCode`.
- Refreshing an already expired token has not been verified. A long Home Assistant outage may therefore require importing a new token.
- New indoor units added after initial setup are not dynamically registered in version 0.1.5.
- The master-auto direction lamp is not present in the captured cloud response. Direction inference and opposite-direction rejection behavior should be rechecked on other GMV generations.
- Fan command values `1..6` and reported execution values `3..7` have different meanings. The five fixed execution levels were calibrated on the tested installation; an unconfigured target deliberately defaults to automatic rather than being inferred from execution speed.
- The official mini-program can write automatic fan and the wired controller applies it, but after leaving the page the mini-program cannot display that target again. Home Assistant likewise has no server field from which to recover an automatic target and therefore persists only its own most recent explicit selection.
- Because the cloud reports execution level but not target type, a wired-controller change from a fixed target to automatic cannot be distinguished from a change to the currently executing fixed level. Select Auto in Home Assistant/HomeKit once after making that particular external change.

## Removal

Remove the integration from **Settings → Devices & services**, uninstall it in HACS, and restart Home Assistant. To revoke the cloud session immediately, log out of the official mini-program or use any account-session controls Gree makes available.

## License

MIT
