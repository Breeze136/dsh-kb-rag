# Troubleshooting

Start with the installed version. Most reports come from an old profile entry, not from the
engine.

## Check what is installed

DSH desktop, Windows PowerShell:

```powershell
(Get-Content "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-kb-rag\package.json" -Raw |
  ConvertFrom-Json).version
```

It should print `1.6.8`. If the path is missing, the plugin is not installed in that profile.
On macOS or Linux, replace `$env:USERPROFILE` with `$HOME`.

Also check that a new conversation exposes the tools. They are injected when a session is
created, so an old conversation will never show them.

## The panel says `dsh-kb-rag@1.6.7 ... incompatible`

The panel checks the **installed** manifest. An existing 1.6.7 has the old peer range
`@deepseek-ai/dsh-tools ^0.1.0-rc.6`, so the check fails before 1.6.8 is installed. The error
keeps quoting 1.6.7 even though the card says 1.6.8.

Fix:

1. Quit DSH.
2. Reopen it, go to **Settings → Plugins**.
3. Uninstall `dsh-kb-rag`.
4. Restart DSH, then install `kb-rag` again.

If the panel still quotes 1.6.7, clear its HTTP cache:

```powershell
$d = "$env:APPDATA\@deepseek-ai\dsh-desktop"
Remove-Item -Recurse -Force "$d\Cache","$d\Code Cache","$d\GPUCache","$d\Network" -ErrorAction SilentlyContinue
```

## `Cannot find package ...\dsh-kb-rag\index.js`

The plugin was installed from the GitHub repository root, not from npm. The repository root
is a manifest shim; the entry point lives in `npm-package/lib/index.js`.

Fix: uninstall that entry, then install `dsh-kb-rag@1.6.8` from npm in the plugin panel. Do
not choose the `github:` source for normal use.

## The panel shows `.hotload\...\package.json` missing

That is stale metadata from an earlier local preview. It does not block a normal install by
itself.

Fix: uninstall the plugin, restart DSH, install 1.6.8 again. If the message survives, clear
the cache as in the 1.6.7 section.

## Install succeeds but there are no `kb_*` tools

Check, in order:

1. The installed version is 1.6.8 (command above).
2. DSH was restarted after installing.
3. The conversation is new. Tools are injected at session creation.
4. The desktop profile lists the bundle. In
   `~/.dsh/profiles/desktop/package.json`, `dsh.profile.bundles` should contain
   `dsh-kb-rag`.

If all four hold, open a new conversation and call `kb_stats`. A response with document
counts means the plugin is loaded.

## `desktop` profile is managed exclusively by the Electron application

That is expected. The desktop profile cannot be changed with `dsh plugin`; use
**Settings → Plugins**. The CLI route is for the web profile only, which is not a supported
install target.

## Python dependencies or models

If a tool reports missing Python packages, run the command printed in the error, or set
`KB_AUTO_PIP=1` so startup installs the pinned dependencies. A missing model is different:
set `HF_ENDPOINT=https://hf-mirror.com` and `KB_HF_MIRROR_FIRST=1`, then retry. `kb_stats`
shows whether each model is present.

## Before opening an issue

Include:

- DSH version (`dsh --version`, or About in the desktop app).
- Plugin version (command at the top).
- Operating system.
- The exact error text, not a paraphrase.
- The output of `kb_stats`, if the plugin loads at all.
- The `dependencies` and `dsh.profile.bundles` sections of the profile `package.json`.
  Remove local paths and account names before posting.

Do not paste `.npmrc`, tokens or full home-directory listings.
