# Upgrading from 1.6.7 to 1.6.8

1.6.7 declares `@deepseek-ai/dsh-tools ^0.1.0-rc.6`, which DSH 0.2 rejects. The plugin panel
reads the **installed** manifest when it checks compatibility, so it can refuse to install
1.6.8 and repeat an error that quotes 1.6.7. The candidate 1.6.8 is already compatible; the
check is looking at the wrong copy.

This affects upgrades, not fresh installs. A profile with no `dsh-kb-rag` installs 1.6.8
normally.

## Web profile

Replace 1.6.7 in one step:

```bash
dsh plugin --profile web add dsh-kb-rag@1.6.8
```

Then restart DSH and open a new session. Verify:

```bash
dsh plugin --profile web list | grep kb-rag
```

The installed `node_modules/dsh-kb-rag/package.json` should report `1.6.8`.

## Desktop profile

The desktop profile is managed by the Electron app, so the CLI refuses it. Use the panel:

1. Quit DSH completely.
2. Reopen DSH and go to **Settings → Plugins**.
3. Uninstall the existing `dsh-kb-rag` entry.
4. Restart DSH once, then install `kb-rag` again. It should resolve to 1.6.8.
5. Restart DSH and start a new conversation.

If the panel still quotes 1.6.7 after the reinstall, clear the app's HTTP cache and retry:

```powershell
$d = "$env:APPDATA\@deepseek-ai\dsh-desktop"
Remove-Item -Recurse -Force "$d\Cache","$d\Code Cache","$d\GPUCache","$d\Network" -ErrorAction SilentlyContinue
```

## Manual fallback for the desktop profile

Only needed when the panel will not install at all. Quit DSH first.

```powershell
$p = "$env:USERPROFILE\.dsh\profiles\desktop"
$j = Get-Content "$p\package.json" -Raw | ConvertFrom-Json
$j.dependencies | Add-Member -NotePropertyName 'dsh-kb-rag' -NotePropertyValue '1.6.8' -Force
if ($j.dsh.profile.bundles -notcontains 'dsh-kb-rag') { $j.dsh.profile.bundles += 'dsh-kb-rag' }
$j | ConvertTo-Json -Depth 12 | Set-Content "$p\package.json" -Encoding utf8

cd $p
pnpm install --no-frozen-lockfile --config.confirmModulesPurge=false
```

Start DSH and check that a new conversation exposes `kb_ingest`, `kb_search`, `kb_rag`,
`kb_stats` and the other `kb_*` tools.

## Why this is not fixed in 1.6.8 itself

The installed 1.6.7 manifest cannot be changed retroactively. The panel has to evaluate the
candidate package before the upgrade, or offer an explicit "replace anyway" path; that is a
DSH plugin-manager behaviour, not something a plugin can override from its own manifest.

The practical fix for users is the uninstall/reinstall above. The long-term fix belongs in
DSH: check the version being installed, not only the version already present.
