#!/usr/bin/env python3
"""Build the HHVM change digest as a dependency-free static site."""

from __future__ import annotations

import argparse
import html
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "public"
SOURCE_PATTERN = re.compile(r"^(\d{4})/(\d{2})/(\d{2})\.md$")
LINK_OR_CODE = re.compile(
    r"(?P<code>(?P<ticks>`+)(?P<code_text>.+?)(?P=ticks))"
    r"|(?P<link>\[(?P<label>[^]]+)\]\((?P<url>https?://[^ )]+)\))"
)
COMMIT = re.compile(r"/commit/([0-9a-f]{7,40})", re.IGNORECASE)
STANDARD_EMPTY_MESSAGE = "No noteworthy HHVM changes were recorded for this period."
FORK_EMPTY_MESSAGE = "Changes not yet included in Hershel's HHVM fork"
SUSTAINED_GAP_DAYS = 14

@dataclass(frozen=True)
class Entry:
    day: date
    section: str
    markdown: str
    commits: frozenset[str]


@dataclass(frozen=True)
class DayDocument:
    day: date
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Period:
    key: str
    title: str
    eyebrow: str
    entries: tuple[Entry, ...]


def inline_markdown(value: str) -> str:
    """Render the small inline Markdown subset used by the source files."""
    output: list[str] = []
    cursor = 0
    for match in LINK_OR_CODE.finditer(value):
        output.append(html.escape(value[cursor : match.start()]))
        if match.group("code"):
            code = match.group("code_text")
            # CommonMark strips one padding space from matching code spans.
            if code.startswith(" ") and code.endswith(" ") and code.strip():
                code = code[1:-1]
            output.append(f"<code>{html.escape(code)}</code>")
        else:
            label = html.escape(match.group("label"))
            raw_url = match.group("url")
            url = html.escape(raw_url, quote=True)
            output.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>')
        cursor = match.end()
    output.append(html.escape(value[cursor:]))
    return "".join(output)


def load_documents() -> list[DayDocument]:
    documents: list[DayDocument] = []
    for path in sorted(ROOT.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9].md")):
        relative = path.relative_to(ROOT).as_posix()
        match = SOURCE_PATTERN.fullmatch(relative)
        if not match:
            continue
        day = date(*(int(part) for part in match.groups()))
        section = "Highlights"
        entries: list[Entry] = []
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] != f"# {day.isoformat()}":
            raise ValueError(f"{relative}: expected '# {day.isoformat()}' as the first line")
        for number, line in enumerate(lines[1:], 2):
            if not line or line == "No relevant changes":
                continue
            if line.startswith("## "):
                section = line[3:]
                continue
            if line.startswith("- "):
                markdown = line[2:]
                commits = frozenset(COMMIT.findall(markdown))
                entries.append(Entry(day, section, markdown, commits))
                continue
            raise ValueError(f"{relative}:{number}: unsupported Markdown: {line!r}")
        documents.append(DayDocument(day, tuple(entries)))
    if not documents:
        raise ValueError("No YYYY/MM/DD.md source documents found")
    return documents


def discover_fork_boundary(documents: Sequence[DayDocument]) -> tuple[date | None, int]:
    """Find the first sustained post-2025 gap between noteworthy-change days."""
    notable_days = [document.day for document in documents if document.entries]
    for previous, following in zip(notable_days, notable_days[1:]):
        empty_days = (following - previous).days - 1
        if following.year > 2025 and empty_days >= SUSTAINED_GAP_DAYS:
            return previous + timedelta(days=1), empty_days
    return None, 0


def empty_message(anchor: date, fork_boundary: date | None) -> str:
    if fork_boundary is not None and anchor >= fork_boundary:
        return FORK_EMPTY_MESSAGE
    return STANDARD_EMPTY_MESSAGE


def iso_week_key(day: date) -> tuple[int, int]:
    iso = day.isocalendar()
    return iso.year, iso.week


def week_title(year: int, week: int) -> str:
    monday = date.fromisocalendar(year, week, 1)
    sunday = monday + timedelta(days=6)
    if monday.year == sunday.year:
        span = f"{monday.strftime('%b %-d')}–{sunday.strftime('%b %-d, %Y')}"
    else:
        span = f"{monday.strftime('%b %-d, %Y')}–{sunday.strftime('%b %-d, %Y')}"
    return f"Week {week}: {span}"


def build_periods(documents: Sequence[DayDocument]) -> tuple[list[Period], list[Period]]:
    weeks: dict[tuple[int, int], list[Entry]] = defaultdict(list)
    for document in documents:
        weeks[iso_week_key(document.day)].extend(document.entries)
    week_periods = [
        Period(f"{year}/W{week:02d}", week_title(year, week), str(year), tuple(entries))
        for (year, week), entries in sorted(weeks.items())
    ]

    # Months are assembled through the weekly collections, then clipped to the
    # calendar month. This keeps the same related-commit data model at both levels.
    months: dict[tuple[int, int], list[Entry]] = defaultdict(list)
    for period in week_periods:
        for entry in period.entries:
            months[(entry.day.year, entry.day.month)].append(entry)
    # Include months containing only no-change days.
    for document in documents:
        months.setdefault((document.day.year, document.day.month), [])
    month_periods = [
        Period(f"{year}/{month:02d}", date(year, month, 1).strftime("%B %Y"), str(year), tuple(entries))
        for (year, month), entries in sorted(months.items())
    ]
    return week_periods, month_periods


def nav_link(href: str | None, direction: str, label: str) -> str:
    arrow = "←" if direction == "prev" else "→"
    contents = f'<span aria-hidden="true">{arrow}</span><span>{html.escape(label)}</span>'
    if href:
        return f'<a class="period-button {direction}" rel="{direction}" href="{href}">{contents}</a>'
    return f'<span class="period-button {direction} disabled" aria-disabled="true">{contents}</span>'


def page_shell(title: str, description: str, active: str, content: str) -> str:
    digest_links = []
    for name, href, label in (
        ("daily", "/daily/", "Daily"),
        ("weekly", "/weekly/", "Weekly"),
        ("monthly", "/monthly/", "Monthly"),
    ):
        current = ' aria-current="page"' if active == name else ""
        digest_links.append(f'<li><a href="{href}"{current}>{label} digests</a></li>')
    digest_nav = "".join(digest_links)
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{html.escape(description, quote=True)}">
  <title>{html.escape(title)} | HHVM Change Digest</title>
  <link rel="stylesheet" href="/static/css/main.css">
  <link rel="icon" href="/static/logo.svg" type="image/svg+xml">
</head>
<body class="docsNavVisible">
  <div class="fixedHeaderContainer">
    <div class="headerWrapper wrapper">
      <header>
        <a class="brand" href="/" aria-label="HHVM Change Digest home"><h2>HOME</h2></a>
      </header>
    </div>
  </div>
  <div class="navPusher">
    <div class="docMainWrapper wrapper">
      <aside class="docsNavContainer">
        <nav class="toc" aria-label="Change digest navigation">
          <div class="navBreadcrumb"><strong>Change Digests</strong></div>
          <div class="navGroups">
            <section class="navGroup navGroupActive"><h3>Change Digests</h3><ul>{digest_nav}</ul></section>
          </div>
        </nav>
      </aside>
      <main class="mainContainer blogContainer postContainer"><div class="mainWrapper">{content}</div></main>
    </div>
    <footer class="footerContainer"><div class="footerWrapper wrapper"><p>This community documentation is not affiliated with Meta.</p></div></footer>
  </div>
</body>
</html>
'''


def change_count(count: int) -> str:
    return f'{count} {"change" if count == 1 else "changes"}'


def period_header(
    eyebrow: str,
    title: str,
    summary: str,
    switch_links: str,
    navigation: str = "",
    sub_summary: str = "",
) -> str:
    switcher_html = f'<nav class="period-switcher" aria-label="View this date by period">{switch_links}</nav>' if switch_links else ""
    sub_summary_html = f'<p class="post-submeta">{html.escape(sub_summary)}</p>' if sub_summary else ""
    return f'''<header class="post-header hero">
  <div class="authorPhoto"><img src="/static/logo.svg" alt=""></div>
  <p class="post-authorName">{html.escape(eyebrow)}</p>
  <h1 class="post-title">{html.escape(title)}</h1>
  <p class="post-meta">{html.escape(summary)}</p>
  {sub_summary_html}
  {switcher_html}
  {navigation}
</header>'''


def switcher(day: date, active: str) -> str:
    week_year, week = iso_week_key(day)
    links = (
        ("daily", f"/daily/{day:%Y/%m/%d}/", "Day"),
        ("weekly", f"/weekly/{week_year}/W{week:02d}/", "Week"),
        ("monthly", f"/monthly/{day:%Y/%m}/", "Month"),
    )
    return "".join(
        f'<a href="{href}"' + (' aria-current="page"' if name == active else "") + f'>{label}</a>'
        for name, href, label in links
    )


def group_by_source_section(entries: Sequence[Entry]) -> list[tuple[str, list[Entry]]]:
    """Group by source h2, keeping shared commits adjacent within each h2."""
    sections: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        sections[entry.section].append(entry)

    grouped: list[tuple[str, list[Entry]]] = []
    # Dict insertion order is the first appearance of each h2 in the underlying
    # chronological daily documents, so no new section ordering is imposed.
    for section, values in sections.items():
        parent = list(range(len(values)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        commit_owner: dict[str, int] = {}
        for index, entry in enumerate(values):
            for commit in entry.commits:
                if commit in commit_owner:
                    union(index, commit_owner[commit])
                else:
                    commit_owner[commit] = index

        components: dict[int, list[Entry]] = defaultdict(list)
        for index, entry in enumerate(values):
            components[find(index)].append(entry)
        ordered_components = sorted(
            components.values(),
            key=lambda component: min(entry.day for entry in component),
        )
        ordered = [
            entry
            for component in ordered_components
            for entry in sorted(component, key=lambda item: (item.day, item.markdown))
        ]
        grouped.append((section, ordered))
    return grouped


def entry_item(entry: Entry, source_kind: str) -> str:
    day_href = f"/daily/{entry.day:%Y/%m/%d}/"
    if source_kind == "week":
        source = f'<a href="{day_href}">{entry.day.strftime("%a, %b %-d")}</a>'
    else:
        week_year, week = iso_week_key(entry.day)
        source = (
            f'<a href="/weekly/{week_year}/W{week:02d}/">W{week:02d}</a>'
            f'<span aria-hidden="true"> · </span><a href="{day_href}">{entry.day.strftime("%b %-d")}</a>'
        )
    return f'''<li class="change-entry">
  <div class="entry-meta">{source}</div>
  <div class="entry-copy">{inline_markdown(entry.markdown)}</div>
</li>'''


def grouped_entries(entries: Sequence[Entry], source_kind: str, message: str = STANDARD_EMPTY_MESSAGE) -> str:
    if not entries:
        return f'''<section class="empty-state">
  <span aria-hidden="true">✓</span><h2>No relevant changes</h2>
  <p>{html.escape(message)}</p>
</section>'''
    parts = ['<div class="subject-groups">']
    for section, values in group_by_source_section(entries):
        parts.append(
            f'<section class="subject-group"><header><h2>{html.escape(section)}</h2>'
            f'<span>{len(values)} {"change" if len(values) == 1 else "changes"}</span></header><ul>'
        )
        parts.extend(entry_item(entry, source_kind) for entry in values)
        parts.append("</ul></section>")
    parts.append("</div>")
    return "".join(parts)


def daily_entries(document: DayDocument, message: str) -> str:
    if not document.entries:
        return grouped_entries((), "week", message)
    by_section: dict[str, list[Entry]] = defaultdict(list)
    for entry in document.entries:
        by_section[entry.section].append(entry)
    parts = ['<div class="daily-sections">']
    for section in by_section:
        parts.append(f'<section class="daily-section"><h2>{html.escape(section)}</h2><ul>')
        for entry in by_section[section]:
            parts.append(f'<li class="change-entry"><div class="entry-copy">{inline_markdown(entry.markdown)}</div></li>')
        parts.append("</ul></section>")
    parts.append("</div>")
    return "".join(parts)


def pagination(previous: str | None, following: str | None, unit: str) -> str:
    return (
        '<nav class="period-pagination" aria-label="Period navigation">'
        + nav_link(previous, "prev", f"Previous {unit}")
        + nav_link(following, "next", f"Next {unit}")
        + "</nav>"
    )


def render_daily(
    document: DayDocument,
    previous: str | None,
    following: str | None,
    fork_boundary: date | None,
) -> str:
    title = document.day.strftime("%A, %B %-d, %Y")
    count = len(document.entries)
    message = empty_message(document.day, fork_boundary)
    summary = (
        f'{count} noteworthy {"change" if count == 1 else "changes"} recorded on this day.'
        if count
        else message if message == FORK_EMPTY_MESSAGE else "No noteworthy changes were recorded on this day."
    )
    content = (
        '<div class="page-wrapper posts"><article class="post digest-post">'
        + period_header(
            "Daily digest",
            title,
            summary,
            switcher(document.day, "daily"),
            pagination(previous, following, "day"),
        )
        + '<div class="post-content digest-card">'
        + daily_entries(document, message)
        + "</div>"
        + "</article></div>"
    )
    return page_shell(title, summary, "daily", content)


def period_anchor(period: Period, kind: str) -> date:
    if kind == "weekly":
        year, week = period.key.split("/W")
        return date.fromisocalendar(int(year), int(week), 1)
    year, month = period.key.split("/")
    return date(int(year), int(month), 1)


def render_period(
    period: Period,
    previous: str | None,
    following: str | None,
    kind: str,
    fork_boundary: date | None,
) -> str:
    anchor = period_anchor(period, kind)
    if kind == "weekly" and period.entries:
        # The archive may start mid-week; choose its first available source day
        # rather than linking to a day before the dataset begins.
        anchor = max(anchor, min(entry.day for entry in period.entries))
    unit = "week" if kind == "weekly" else "month"
    count = len(period.entries)
    message = empty_message(anchor, fork_boundary)
    summary = (
        f'{count} noteworthy {"change" if count == 1 else "changes"}, organized by the source changelog sections.'
        if count
        else message if message == FORK_EMPTY_MESSAGE else f"No noteworthy changes were recorded for this {unit}."
    )
    content = (
        '<div class="page-wrapper posts"><article class="post digest-post">'
        + period_header(
            f"{unit.title()}ly digest",
            period.title,
            summary,
            switcher(anchor, kind),
            pagination(previous, following, unit),
        )
        + '<div class="post-content">' + grouped_entries(period.entries, unit, message) + "</div>"
        + "</article></div>"
    )
    return page_shell(period.title, summary, kind, content)


def period_index(periods: Sequence[Period], kind: str) -> str:
    unit = "week" if kind == "weekly" else "month"
    cards = []
    for period in reversed(periods):
        archive_title = re.sub(r",? \d{4}", "", period.title)
        inactive = ' class="inactive-period"' if not period.entries else ""
        cards.append(
            f'<li{inactive}><a href="/{kind}/{period.key}/"><span>{html.escape(period.eyebrow)}</span>'
            f'<strong>{html.escape(archive_title)}</strong><small>{change_count(len(period.entries))}</small></a></li>'
        )
    content = f'''<div class="page-wrapper posts"><article class="post archive-post">
  {period_header("Browse the archive", f"{kind.title()} digests", f"Every {unit} of HHVM changes, organized for quick scanning.", "")}
  <div class="post-content"><ol class="archive-list">{''.join(cards)}</ol></div>
</article></div>'''
    return page_shell(f"{kind.title()} archive", f"Browse all {kind} HHVM change digests.", kind, content)


def daily_index(documents: Sequence[DayDocument]) -> str:
    cards = []
    for document in reversed(documents):
        inactive = ' class="inactive-period"' if not document.entries else ""
        cards.append(
            f'<li{inactive}><a href="/daily/{document.day:%Y/%m/%d}/"><span>{document.day.year}</span>'
            f'<strong>{document.day.strftime("%A, %B %-d")}</strong>'
            f'<small>{change_count(len(document.entries))}</small></a></li>'
        )
    content = f'''<div class="page-wrapper posts"><article class="post archive-post">
  {period_header("Browse the archive", "Daily digests", "Every source day, including quiet days with no relevant changes.", "")}
  <div class="post-content"><ol class="archive-list">{''.join(cards)}</ol></div>
</article></div>'''
    return page_shell("Daily archive", "Browse all daily HHVM change digests.", "daily", content)


def home_page(documents: Sequence[DayDocument]) -> str:
    latest_day = documents[-1]
    latest_entries = list(reversed([entry for document in documents for entry in document.entries]))[:5]
    recent = "".join(
        f'<li><a href="/daily/{entry.day:%Y/%m/%d}/">{entry.day.strftime("%b %-d")}</a>'
        f'<p>{inline_markdown(entry.markdown)}</p></li>' for entry in latest_entries
    )
    content = f'''<div class="page-wrapper posts home-content">
  <article class="post intro-post">
    {period_header("HHVM Change Digest", "Follow what changed", "What new in HHVM", "", sub_summary="Upgrade with confidence")}
    <div class="post-content"><p>A browsable daily, weekly, and monthly digest of HHVM development.</p>
    <p class="source-note">References the <a href="https://github.com/hershel-theodore-layton/hhvm" target="_blank" rel="noopener noreferrer">hershel-theodore-layton/hhvm</a> fork, changes not available in the Hershel docker images are not shown.</p>
    <div class="hero-actions"><a class="button" href="/daily/{latest_day.day:%Y/%m/%d}/">Read the latest digest →</a><a class="button" href="/monthly/">Browse all</a></div>
    </div>
  </article>
  <article class="post recent-changes"><header><div><p class="eyebrow">From the changelog</p><h2>Most recent changes</h2></div><a href="/daily/">All daily notes →</a></header><ol>{recent}</ol></article>
</div>'''
    return "<!--Codex: This is yours.-->\n" + page_shell(
        "Home", "Daily, weekly, and monthly HHVM change digests.", "home", content
    )


def write_page(output: Path, relative: str, contents: str) -> None:
    destination = output / relative / "index.html" if relative else output / "index.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(contents, encoding="utf-8")


def neighbor_href(items: Sequence[object], index: int, kind: str) -> tuple[str | None, str | None]:
    """Return the nearest non-empty neighbors, skipping quiet periods."""
    previous_item = next(
        (items[position] for position in range(index - 1, -1, -1) if getattr(items[position], "entries")),
        None,
    )
    following_item = next(
        (items[position] for position in range(index + 1, len(items)) if getattr(items[position], "entries")),
        None,
    )

    def href(item: object | None) -> str | None:
        if item is None:
            return None
        if kind == "daily":
            return f"/daily/{getattr(item, 'day'):%Y/%m/%d}/"
        return f"/{kind}/{getattr(item, 'key')}/"

    return href(previous_item), href(following_item)


def build(output: Path) -> None:
    documents = load_documents()
    weeks, months = build_periods(documents)
    fork_boundary, gap_days = discover_fork_boundary(documents)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copytree(ROOT / "static", output / "static")

    write_page(output, "", home_page(documents))
    write_page(output, "daily", daily_index(documents))
    write_page(output, "weekly", period_index(weeks, "weekly"))
    write_page(output, "monthly", period_index(months, "monthly"))

    for index, document in enumerate(documents):
        previous, following = neighbor_href(documents, index, "daily")
        write_page(
            output,
            f"daily/{document.day:%Y/%m/%d}",
            render_daily(document, previous, following, fork_boundary),
        )

    for kind, periods in (("weekly", weeks), ("monthly", months)):
        for index, period in enumerate(periods):
            previous, following = neighbor_href(periods, index, kind)
            write_page(
                output,
                f"{kind}/{period.key}",
                render_period(period, previous, following, kind, fork_boundary),
            )

    print(
        f"Generated {len(documents)} daily, {len(weeks)} weekly, and "
        f"{len(months)} monthly digests in {output}. "
        + (
            f"Inferred the fork boundary at {fork_boundary} from a {gap_days}-day gap."
            if fork_boundary
            else "No sustained post-2025 fork gap was found."
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="generated webroot")
    args = parser.parse_args()
    build(args.output.resolve())


if __name__ == "__main__":
    main()
