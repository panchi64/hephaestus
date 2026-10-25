# Plugins

A plugin is a self-contained feature: a name, some metadata, and a list of **contributions**. There are two tiers, and both use the same contribution types.

| | Built-in | Sandboxed |
|---|---|---|
| Lives in | `backend/plugins/<name>/` (+ `frontend/src/plugins/<name>/`) | an encrypted bundle row, written by the agent or imported |
| Runs | in-process, as a `FeatureManifest` with `plugin=PluginInfo(...)` | tools in a VM per call; windows in a sandboxed iframe |
| Declares contributions | as Python objects on `FeatureRuntime.contributions` | in `plugin.yaml` and its files |
| Switched on/off | the Plugins screen (`PUT /plugins/builtin/{name}`) | publishing, or automatically when inert |

Mail, Routines and Discord are built-ins. Nothing outside `backend/plugins/` imports a built-in, and no built-in imports another; `tests/test_builtin_plugins.py` enforces it.

## Contribution types

Every kind of thing a plugin can add is a `ContributionType` registered in `services/plugins/contributions/`. A module's presence there is its registration. The manifest parser, the store, the built-in loader and the Plugins screen loop over the registry; none of them names a type.

A type declares:

- `key`, its `plugin.yaml` key and the label the Plugins screen groups items under;
- `tiers`, whether sandboxed plugins may use it or only built-ins;
- `trust`: `inert` types only show the operator something, `privileged` types can act. A sandboxed plugin whose every contribution is inert, with no requirements, egress or secrets, goes live as soon as it is written (`PluginStore.publish_if_inert`). Anything else waits for the operator to publish it;
- `parse` for sandboxed bundles, `describe` for the Plugins screen, and `start`/`stop` for what switching a built-in on and off does.

| Key | Tiers | Trust | What it adds |
|---|---|---|---|
| `tools` | both | privileged | Python tools, each call run in the plugin's VM |
| `agents` | both | privileged | agent definitions from `agents/<name>.md` |
| `skills` | both | privileged | skill bundles from `skills/<name>/SKILL.md` |
| `routines` | both | privileged | routine templates (registered by the Routines plugin) |
| `windows` | both | inert | pages under `ui/` that open in a floating window |
| `permissions` | both | inert | which bridge methods those windows may call |
| `background` | built-in | privileged | a loop started and stopped with the plugin |
| `alert_channels` | built-in | privileged | an out-of-band notification channel (email) |
| `channels` | built-in | privileged | a messaging platform (Discord) on the channel hub |

### Adding a contribution type

1. Add a module in `services/plugins/contributions/` with a `TypedContribution[Item]` subclass and `CONTRIBUTIONS.register(...)`. Override `parse` if sandboxed plugins may declare it, `start`/`stop` if switching a built-in matters, and `describe_one` for the Plugins screen.
2. If it shows UI, add a renderer on the frontend registry (`frontend/src/plugins/`).
3. If it persists data, own the table in that module's area and delete its rows with the plugin.
4. Add a test that parses it and, for built-ins, toggles a plugin carrying it.

The test in `tests/test_builtin_plugins.py` registers a fake type and drives it through the loader, which guards the "new type is one module" rule.

## Built-in plugins

`harness/discovery.py` walks `backend/plugins/*` beside the core manifests; every manifest there must set `plugin`, and no core manifest may. A built-in declares into core's tables through its manifest (`tool_sensitivity`, `projections`, `personal_categories`, `credentials`, `run_kinds`, `out_of_band_kinds`, `platform_sources`), so core never names it.

Switching one off (`harness/builtin_plugins.py`) makes it idle, not unloaded: its routes return 404, its toolsets and prompt providers go quiet, and each contribution's `stop` runs. Its services stay built so a parked run can still resume. The switch is stored in `SettingsStore` under `plugins.builtin.<name>.enabled`.

Manifests that compose agent turns set `composes_turns`; discovery orders them after every other plugin so the turns they compose see the full catalog.

## Plugin windows

A window is a page from the plugin's `ui/` folder:

```yaml
api: 1
name: snake
description: Snake, to play while the agent works.
windows:
  - id: main
    title: Snake
    entry: ui/index.html
    size: {width: 360, height: 420}
permissions: [storage, chat.read]
```

`windows` checks the assets: only listed file types, text in UTF-8, images whose bytes match their extension, and nothing under a `__` name.

Opening a window (`POST /plugins/{id}/windows/{window}/sessions`) mints two tokens in `WindowSessions`:

- the **page token** is in the frame's URL and only serves that window's files from `/plugin-ui/{token}/…`. The plugin's code can read it, and it grants nothing else;
- the **bridge token** stays with the host app, which sends it as `X-Plugin-Window` on each bridge call it makes for the window. The plugin never sees it.

Both close when the plugin is changed, switched off or deleted, and when the vault locks.

Every page response carries `Content-Security-Policy: sandbox allow-scripts; … connect-src 'none'; frame-ancestors <app origins>`, `nosniff`, `no-referrer` and `no-store`. The page runs in an opaque origin wherever it is opened and has no network. `/plugin-ui` skips the cookie gate because the token is the credential, but stays behind the vault gate.

The agent opens a window with `plugins_open_window`, which emits `plugin.window_open`. The client opens it only for the conversation on screen, and not on a replay. The header's launcher lists every live plugin's windows. Open windows and their geometry are kept in `localStorage`.

## The bridge

The page loads `__bridge.js` (`services/plugins/ui_bridge.js`), which defines `window.odysseus`. On each frame load the host hands the page one `MessagePort` and every call goes over it. The host broker (`frontend/src/app/plugin-windows/bridge/`) validates each request, rate-limits it, answers client-side methods itself (`window.close`, `chat.open`), and forwards the rest to `POST /plugins/bridge/{method}`.

The route reads the plugin from the bridge token, checks that the plugin declared the method's permission, rate-limits per window and method, and asks the capability whether the operator must confirm. A confirmed call comes back as `outcome: confirm` with an id. Its parameters are parked server-side, so the operator confirms exactly what was asked, and the id is good once, for that window, for five minutes. "Always allow" is stored with the digest of the plugin version it was given to, so rewritten code asks again.

| Method | Permission | Confirms |
|---|---|---|
| `storage.get/set/delete/list` | `storage` | never |
| `tools.call` | `tools` | when the tool declares `external_effect` |
| `chat.post` | `chat.post` | always |
| events `agent.status`, `agent.turn_finished` | `chat.read` | (client-side) |

Storage is sealed per plugin: a key is at most 128 characters, a value at most 64 KB of JSON, and a plugin may keep at most 500 keys and 1 MB in total. Its rows are deleted with the plugin and left out of exports. `tools.call` runs through the same VM path as the agent's call (`services/plugins/calls.py`), with the same egress and secrets.

### Adding a bridge capability

1. Add a module in `services/plugins/bridge/` whose handlers take a `BridgeCall` and return JSON, and register a `BridgeCapability(method, permission, handler, asks=…)`. A new permission name is declared by registering it.
2. `asks` returns a `Confirmation(summary, grant)` when the operator must say yes. `summary` completes "<window> wants to …"; `grant` is what "always allow" remembers.
3. A method the frontend answers itself goes in the broker's `CLIENT_METHODS` instead. Nothing on the frontend changes for a backend method.
4. Add the method to the `plugins_write` docstring so the agent knows it exists, and add a test in `tests/test_plugin_bridge.py`.

## Messaging channels

`services/channels/` lets the operator talk to the agent from another platform. A channel speaks only its platform (`MessagingChannel`). The `ChannelHub` built by `harness/manifests/channels.py` owns everything shared:

- the per-channel allowlist of sender ids (anyone else is ignored);
- `channel_links`: one rolling conversation per address, until `/new` there;
- inbound messages start or queue a turn through `send_operator_message`, the same path as the app's composer;
- a run-terminal hook sends each linked turn's ending back: the answer, or one line when it failed, stopped or hit a limit;
- approvals ride the `approval_needed` notification. The hub shows the run's pending calls on every linked address (or the channel's fallback), and a button decides them all through `answer_approval`. That request carries no operator session, so a guarded call is refused and the operator is pointed to the app. A button whose calls were already decided says so.

### Adding a messaging channel

1. Add a built-in plugin under `backend/plugins/<platform>/` with a class implementing `MessagingChannel`. It turns incoming messages into `hub.inbound(...)` and buttons into `hub.decide(...)`, splits long messages in `send`, and renders `request_approval` its own way.
2. Contribute it as `Messaging(hub, channel)` so switching the plugin connects and disconnects it.
3. Declare its credential, keep its own settings in `SettingsStore`, and add a settings section on the frontend registry.
4. Test it with a fake client. `tests/test_channels.py` covers the shared behaviour with an in-memory channel.
