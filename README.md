# globus-usable

A small wrapper around `globus` (Globus CLI) whose UX mirrors `rsync` for local↔remote transfers (including trailing-slash behavior).

## Installation

This tool shells out to the Globus CLI, so you’ll need a working `globus` in your environment.

```bash
pip install 'globus-usable @ git+https://github.com/chaichontat/globus-usable.git'
```

Then authenticate:

```bash
globus login
```

If Globus tells you consent is required for a collection, run the `globus session consent ...` command it prints.
The dependent `data_access` scope typically looks like:

```bash
globus session consent 'urn:globus:auth:scope:transfer.api.globus.org:all[*https://auth.globus.org/scopes/COLLECTION_UUID/data_access]'
```

## Usage

Copy local → remote:

```bash
globus-usable cp ./data.txt dsai:/data/
```

Copy remote → local:

```bash
globus-usable cp dsai:/data/results.csv ./downloads/
```

Copy a directory (recursive):

```bash
globus-usable cp -r ./mydir/ dsai:/data/mydir/
```

Trailing slash mirrors `rsync`:

- `src/ dst/` copies the *contents* of `src/` into `dst/`
- `src dst/` copies the `src` directory *into* `dst/` (i.e. `dst/src/`)

List remote or local:

```bash
globus-usable ls
globus-usable ls -l dsai:/data/
```

Show active transfers (or latest):

```bash
globus-usable status
globus-usable status --live
```

Cancel a transfer:

```bash
globus-usable cancel <task-id>
globus-usable cancel --all
```

## Configuration

By default the config is read from:

- `$XDG_CONFIG_HOME/globus-usable/remotes.toml`, or
- `~/.config/globus-usable/remotes.toml`

Example `remotes.toml`:

```toml
[local]
endpoint_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

[remotes.dsai]
endpoint_id = "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"

[defaults]
default_remote = "dsai"
sync_level = "mtime"         # exists | size | mtime | checksum
poll_interval_min = 2
poll_interval_max = 30
```

Notes:

- Remote paths use `<remote>:/path` (e.g. `dsai:/data/file.txt`). A relative remote path is treated as `~/...`.
- If `[local].endpoint_id` is missing, `THIS_GLOBUS` is used as a fallback.
- If `defaults.default_remote` is requested but missing from `[remotes]`, `THAT_GLOBUS` is used as a fallback endpoint id.
