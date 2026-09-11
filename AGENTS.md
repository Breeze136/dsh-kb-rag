# Repository conventions

## Before every public release

Applies to `npm publish`, GitHub releases and tags, and anything committed to this public
repository — README, docs reached through links, code comments, tool descriptions, tool
schemas, and CHANGELOG.

**Every example must be free of identifying information.** Rules, in order of how often
they get violated:

1. **Use graphene subject matter for all examples.** Sample documents, rendered-output
   blocks, search prompts, answer prose and tool-description examples describe graphene
   growth and characterisation (CVD on copper, domain coalescence, Raman thickness
   probing). Do not use subject matter from the maintainer's own research field, and do
   not reuse a paper from a personal library as an example.
2. **No real papers.** Titles, authors, journals, years, DOIs and Zotero item keys are
   placeholders: `Author A; Author B`, `Carbon · 2024`, `10.5555/12345678`, `<ITEM-KEY>`.
3. **No real names or usernames**, including inside paths (`C:\Users\<username>\...`),
   reproduction environments, and quoted terminal output.
4. **No personal paths or accounts.** Local absolute paths, Zotero library paths, email
   addresses, tokens and credential files stay out of the repository.
5. **No device or institution details.** Machine models, institutional network notes and
   directory listings that reveal a personal workflow do not belong in published docs.

Scan tracked files, not just the README — `git ls-files` is the boundary, and files
linked from the README are the ones most often missed:

```bash
# real usernames in paths, home directories, real Zotero item keys, real DOIs
git ls-files -z | xargs -0 grep -nEi \
  'C:\\\\Users\\\\[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+' \
  -e 'zotero://open-pdf/library/items/(?!EXAMPLEKEY)' \
  -e '10\.[0-9]{4,9}/' | grep -v '10\.5555/\|10\.0000/'
```

Also read the example blocks by eye: a real paper, a research term, or a quoted
terminal session is exactly what a keyword scan can miss.

When in doubt, replace with a neutral placeholder rather than keeping the real value.
This is a fixed step of the release routine; it does not need to be requested.

## After publishing a release

1. **Installing a fresh release into a DSH profile needs the exact version.** pnpm 11
   applies a supply-chain policy (`minimumReleaseAge`): a version published minutes ago
   is "too young", so `dsh plugin --profile <name> add dsh-kb-rag@latest` quietly
   resolves to the newest version that is old enough instead — it looks like it worked
   while nothing changed. Always add the explicit version:

   ```bash
   dsh plugin --profile web add dsh-kb-rag@<version>
   ```

   pnpm then appends that version to `minimumReleaseAgeExclude` in the profile's
   `pnpm-workspace.yaml`. Afterwards, verify three things: the installed
   `node_modules/dsh-kb-rag/package.json` version, the `dsh.profile.bundles` entry, and
   that the bundled `kb_engine.py` still matches the repository copy byte for byte.

2. **Verify the npm page from the packument, not the version document.**
   The registry no longer stores a per-version `readme` field, so
   `registry.npmjs.org/dsh-kb-rag/<version>.readme` is empty even on a good publish.
   The page renders `readme` from the top-level packument:
   `https://registry.npmjs.org/dsh-kb-rag` — check it for the new content, and expect
   `dist-tags.latest` to lag a minute or two behind the publish.

