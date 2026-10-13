---
name: build-a-plugin
description: Write a plugin, which adds Python tools that run in the platform VM and can also contribute agents, skills and routine templates, then test it and hand it to the operator to publish. Use when a task needs a capability no existing tool provides.
---

# Build a plugin

A plugin adds tools you write in Python. They run in the platform VM, in the plugin's own virtualenv, and reach only the hosts the plugin declares. Write one when a capability will be needed again: an API you call repeatedly, or a format you keep converting. For a one-off, run code instead.

## 1. Check what exists

Search first (`platform_search`, sources `plugins`). Load the `plugins` tool group with `search_tools`. Extending an existing plugin beats writing a second one.

## 2. Write it with `plugins_write`

`files` maps each path to its full text. At the root, `plugin.yaml`:

```yaml
name: weather
description: Forecasts from Open-Meteo.
requirements: [httpx]           # pip lines
egress: [api.open-meteo.com]    # the only hosts its code can reach
secrets:                        # the operator sets the values
  OPEN_METEO_KEY: api.open-meteo.com
tools:
  - name: forecast
    description: The forecast for a city.
    entry: weather:forecast     # module:function
    sensitivity: read           # read | workspace_write | external_effect (default)
    parameters:
      type: object
      properties: {city: {type: string}}
      required: [city]
```

Then the modules. A tool function takes its parameters as keyword arguments, may be async, and returns JSON-able data.

- Write each tool's `description` for the model that will call it: what it does and when to use it.
- Use `parameters` as a tight JSON Schema, with `required` set.
- Declare the narrowest `sensitivity` that is true. `external_effect` asks the operator before every call.
- List only the hosts the code calls under `egress`.
- A secret's environment variable holds a placeholder that becomes the real value only in the URL or headers of HTTPS requests to its own host. Never log it, and never send it anywhere else.

A plugin can also contribute:

- **Agents**: `agents/<name>.md`, an agent file with frontmatter `name`, `description` and optionally `tools`, `permission`, `workspace`; the body is its brief.
- **Skills**: `skills/<name>/SKILL.md`, with any files beside it.
- **Routine templates**: `routines/<name>.md`, with frontmatter `title`, `schedule` (`every 1d`, `cron 0 9 * * 1`, `once <ISO time with offset>`) and optionally `agent`; the body is the prompt.

## 3. Test it with `plugins_test`

Call each tool with realistic arguments, including one that should fail, and read the results. A test reaches only the declared hosts this conversation may also reach. A test that would use a secret asks the operator first. Fix the draft and test again until every tool behaves.

## 4. Hand it over

What you write stays a draft. Only the operator publishes a plugin, after reading it in the Plugins area. Tell them what it adds, which hosts it reaches, which secrets they need to set, and what you tested. Once published, its tools appear as `plugins_<plugin>_<tool>` in the `plugins` group.
