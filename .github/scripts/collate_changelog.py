#!/usr/bin/env python3

import argparse
import os
import re
import sys

import requests

"""Generate a per-repo changelog fragment for the collated release changelog.

For a single repository, this pulls all commits between the previous stable
release tag and the current release tag via the GitHub compare API, categorizes
them by commit-message prefix, and writes a Markdown fragment. The reusable
``ci-collate-changelog`` workflow runs this once per plugin (matrix) and then
stitches the fragments into one alphabetically-sorted changelog.

Category / subsection -> prefix mapping (see CATEGORY_STRUCTURE below):

    Breaking Changes 💥
        API Changes           : API
        Deprecations          : DEPR
    New and Improved! 🚀
        New Features          : NEW
        Improvements          : IMP
    Bug Fixes 🪲              : BUG   (no subsection)
    Maintenance ⚙️
        Testing               : TEST
        Documentation         : DOC
        Dependency Pins       : PIN
        Code Refactorization  : REF
        Miscellaneous         : MAINT

Recognized prefixes are stripped from the rendered line item (only the commit
message, sha link, and author remain). Any commit whose subject does not start
with a recognized prefix (matched as ``PREFIX:`` exactly, including the colon)
lands in an "Uncategorized" section for manual review, with its subject left
intact. Release-machinery commits (REL:/DEV:/LANG:/PREP:) created by the
join-release action are skipped entirely.
"""

API = "https://api.github.com"

# Ordered category structure. Each category has a display name, an emoji, and an
# ordered list of (subsection_name, prefix) pairs. A subsection_name of None
# means the commits are listed directly under the category with no subheader.
# Categories, subsections, and the Uncategorized bucket are only rendered when
# they contain matching commits.
CATEGORY_STRUCTURE = [
    ("Breaking Changes", "💥", [
        ("API Changes", "API"),
        ("Deprecations", "DEPR"),
    ]),
    ("New and Improved!", "🚀", [
        ("New Features", "NEW"),
        ("Improvements", "IMP"),
    ]),
    ("Bug Fixes", "🪲", [
        (None, "BUG"),
    ]),
    ("Maintenance", "⚙️", [
        ("Testing", "TEST"),
        ("Documentation", "DOC"),
        ("Dependency Pins", "PIN"),
        ("Code Refactorization", "REF"),
        ("Miscellaneous", "MAINT"),
    ]),
]

# all prefixes that map to a category/subsection above
KNOWN_PREFIXES = {
    prefix for _, _, subs in CATEGORY_STRUCTURE for _, prefix in subs
}

# release-machinery commits created by the join-release action; these carry an
# empty "[skip ci]" payload and are excluded from the changelog entirely.
SKIP_PREFIXES = {"REL", "DEV", "LANG", "PREP"}

# match "PREFIX:" exactly (uppercase letters followed by a colon)
PREFIX_RE = re.compile(r"^([A-Z]+):")
# stable release tag, e.g. 2026.7.0 (dev tags look like 2026.7.0.dev0)
VERSION_RE = re.compile(r"^(\d{4})\.(\d+)\.(\d+)$")


def gh_get(url, token, params=None):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp


def parse_version(tag):
    m = VERSION_RE.match(tag)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def get_stable_tags(repo, token):
    """Return a list of (version_tuple, tag_name) for stable X.Y.Z tags."""
    tags = []
    page = 1
    while True:
        resp = gh_get(
            f"{API}/repos/{repo}/tags",
            token,
            params={"per_page": 100, "page": page},
        )
        batch = resp.json()
        if not batch:
            break
        for t in batch:
            name = t["name"]
            if "dev" in name:
                continue
            version = parse_version(name)
            if version is not None:
                tags.append((version, name))
        if len(batch) < 100:
            break
        page += 1
    return tags


def find_previous_tag(stable_tags, head_version):
    """Return the greatest stable tag strictly less than head_version."""
    candidates = [t for t in stable_tags if t[0] < head_version]
    if not candidates:
        return None
    return max(candidates, key=lambda t: t[0])[1]


def get_commits(repo, base, head, token):
    """Return a list of (short_sha, subject, html_url, login, author_url).

    If ``base`` is None (no prior release tag exists) all commits reachable from
    ``head`` are returned instead.

    NOTE: the compare API returns at most 250 commits per response; releases that
    span more than 250 commits would be truncated. This has not been an issue for
    per-plugin release cycles, but is a known limitation.
    """
    if base:
        resp = gh_get(f"{API}/repos/{repo}/compare/{base}...{head}", token)
        raw = resp.json().get("commits", [])
    else:
        raw = []
        page = 1
        while True:
            resp = gh_get(
                f"{API}/repos/{repo}/commits",
                token,
                params={"sha": head, "per_page": 100, "page": page},
            )
            batch = resp.json()
            if not batch:
                break
            raw.extend(batch)
            if len(batch) < 100:
                break
            page += 1

    commits = []
    for c in raw:
        subject = c["commit"]["message"].splitlines()[0].strip()
        # c["author"] is the linked GitHub account (may be null for commits not
        # associated with a GitHub user); fall back to no author link.
        author = c.get("author") or {}
        login = author.get("login")
        author_url = author.get("html_url")
        commits.append((c["sha"][:7], subject, c["html_url"], login, author_url))
    return commits


def strip_prefix(subject):
    """Remove a leading ``PREFIX:`` and any following whitespace."""
    return PREFIX_RE.sub("", subject, count=1).lstrip()


def categorize(commits):
    """Group commits by prefix.

    Returns (by_prefix, uncategorized): a dict of prefix -> [entries] for
    recognized prefixes, and a list of entries whose subject has no recognized
    prefix. Skip-listed (release-machinery) commits are dropped.
    """
    by_prefix = {}
    uncategorized = []
    for entry in commits:
        subject = entry[1]
        m = PREFIX_RE.match(subject)
        prefix = m.group(1) if m else None
        if prefix in SKIP_PREFIXES:
            continue
        if prefix in KNOWN_PREFIXES:
            by_prefix.setdefault(prefix, []).append(entry)
        else:
            uncategorized.append(entry)
    return by_prefix, uncategorized


def format_item(entry, strip):
    sha, subject, url, login, author_url = entry
    message = strip_prefix(subject) if strip else subject
    item = f"- {message} ([`{sha}`]({url})"
    if login and author_url:
        item += f" by [{login}]({author_url})"
    item += ")"
    return item


def render(name, base, head, by_prefix, uncategorized):
    header_range = f"{base or '<initial>'} → {head}"
    lines = [f"## {name}", "", f"_Changes in {header_range}_", ""]
    any_content = False

    for category, emoji, subs in CATEGORY_STRUCTURE:
        # build subsection blocks that actually have commits
        sub_blocks = []
        for sub_name, prefix in subs:
            entries = by_prefix.get(prefix)
            if not entries:
                continue
            block = []
            if sub_name:
                block.append(f"##### {sub_name}")
                block.append("")
            block.extend(format_item(e, strip=True) for e in entries)
            block.append("")
            sub_blocks.append(block)
        if not sub_blocks:
            continue
        any_content = True
        lines.append(f"#### {category} {emoji}")
        lines.append("")
        for block in sub_blocks:
            lines.extend(block)

    if uncategorized:
        any_content = True
        lines.append("#### Uncategorized")
        lines.append("")
        lines.extend(format_item(e, strip=False) for e in uncategorized)
        lines.append("")

    if not any_content:
        lines.append("_No changes in this release._")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--name", required=True, help="plugin display name")
    parser.add_argument("--release-tag", required=True, help="e.g. 2026.10.0")
    parser.add_argument("--output", required=True, help="fragment output path")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    head_version = parse_version(args.release_tag)
    if head_version is None:
        print(
            f"::warning::release tag '{args.release_tag}' is not X.Y.Z; "
            f"cannot resolve a previous tag for {args.repo}, using full history",
            file=sys.stderr,
        )

    stable_tags = get_stable_tags(args.repo, token)
    base = find_previous_tag(stable_tags, head_version) if head_version else None

    print(f"{args.name}: comparing {base or '<initial>'}...{args.release_tag}")

    commits = get_commits(args.repo, base, args.release_tag, token)
    by_prefix, uncategorized = categorize(commits)
    fragment = render(args.name, base, args.release_tag, by_prefix, uncategorized)

    with open(args.output, "w") as fh:
        fh.write(fragment)
        if not fragment.endswith("\n"):
            fh.write("\n")

    print(fragment)


if __name__ == "__main__":
    main()
