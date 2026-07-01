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

Prefix -> category mapping (see PREFIX_CATEGORY below):

    Breaking Changes : API, DEPR
    New Features!    : NEW, ENH
    Bug Fixes        : BUG
    Maintenance      : MAINT, TEST, REF, CI, PIN, DOC

Any commit whose subject does not start with one of those prefixes (matched as
``PREFIX:`` exactly, including the colon) lands in an "Uncategorized" section for
manual review. Release-machinery commits (REL:/DEV:) created by the join-release
action are skipped entirely.
"""

API = "https://api.github.com"

# prefix -> category
PREFIX_CATEGORY = {
    "API": "Breaking Changes",
    "DEPR": "Breaking Changes",
    "NEW": "New Features!",
    "ENH": "New Features!",
    "BUG": "Bug Fixes",
    "MAINT": "Maintenance",
    "TEST": "Maintenance",
    "REF": "Maintenance",
    "CI": "Maintenance",
    "PIN": "Maintenance",
    "DOC": "Maintenance",
}

# rendered in this order; empty sections are omitted
CATEGORY_ORDER = [
    "Breaking Changes",
    "New Features!",
    "Bug Fixes",
    "Maintenance",
    "Uncategorized",
]

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
    """Return a list of (short_sha, subject, html_url) between base and head.

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
        commits.append((c["sha"][:7], subject, c["html_url"]))
    return commits


def categorize(commits):
    buckets = {cat: [] for cat in CATEGORY_ORDER}
    for sha, subject, url in commits:
        m = PREFIX_RE.match(subject)
        prefix = m.group(1) if m else None
        if prefix in SKIP_PREFIXES:
            continue
        category = PREFIX_CATEGORY.get(prefix, "Uncategorized")
        buckets[category].append((sha, subject, url))
    return buckets


def render(name, base, head, buckets):
    header_range = f"{base or '<initial>'} → {head}"
    lines = [f"## {name}", "", f"_Changes in {header_range}_", ""]
    any_content = False
    for cat in CATEGORY_ORDER:
        entries = buckets[cat]
        if not entries:
            continue
        any_content = True
        lines.append(f"### {cat}")
        for sha, subject, url in entries:
            lines.append(f"- {subject} ([`{sha}`]({url}))")
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
    buckets = categorize(commits)
    fragment = render(args.name, base, args.release_tag, buckets)

    with open(args.output, "w") as fh:
        fh.write(fragment)
        if not fragment.endswith("\n"):
            fh.write("\n")

    print(fragment)


if __name__ == "__main__":
    main()
