"""
Observation digest.

Per the assignment brief (Section 3.1), we bias toward a perception
mechanism that still works when the surface has no clean DOM -- that's the
common case in the real environment. Rather than handing the LLM raw HTML
(which invites it to author brittle CSS selectors against implementation
details), we extract a semantic, accessibility-tree-style list of
interactive elements: role + accessible name + current value. The LLM picks
a *reference* ("e3") from this list; it never writes a selector itself.

For each element we also compute a ranked locator candidate ladder (role/name
first, then attribute-based CSS, then a positional fallback) and record
which tier actually resolved uniquely. This ladder is exactly what Phase 3's
distiller will freeze into the artifact, and what Phase 4's replay engine
will walk through at replay time -- discovery and replay share the same
locator vocabulary on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LocatorCandidate:
    strategy: str  # "role_name" | "css_name_attr" | "css_id" | "text" | "positional"
    value: str


@dataclass
class ObservedElement:
    ref: str
    role: str
    name: str
    value: str | None
    candidates: list[LocatorCandidate]
    playwright_locator: object  # the resolved Locator, for immediate use this run only
    # For links: the absolute href it will navigate to. For submit buttons:
    # the absolute action URL of their enclosing form. None for elements
    # that don't cause navigation (textboxes, selects). Policy checks a
    # click against THIS url, not the current page -- clicking a link to a
    # mutating route must be classified by where it goes, not where it is.
    target_url: str | None = None


@dataclass
class Observation:
    url: str
    title: str
    text: str  # human-readable digest fed to the LLM (interactive elements + page text)
    elements: dict[str, ObservedElement] = field(default_factory=dict)
    page_text: str = ""  # raw visible text content, for reading non-interactive data


_INTERACTIVE_SELECTOR = (
    "input:not([type=hidden]), textarea, select, button, a[href]"
)


def _accessible_name(page, locator) -> str:
    # aria-label wins if present
    aria = locator.get_attribute("aria-label")
    if aria:
        return aria.strip()

    tag = locator.evaluate("e => e.tagName.toLowerCase()")

    # associated <label for="id">
    el_id = locator.get_attribute("id")
    if el_id:
        label = page.locator(f"label[for='{el_id}']")
        if label.count() == 1:
            text = label.inner_text().strip()
            if text:
                return text

    if tag in ("a", "button"):
        text = locator.inner_text().strip()
        if text:
            return text
        val = locator.get_attribute("value")
        if val:
            return val.strip()

    if tag == "input":
        input_type = (locator.get_attribute("type") or "text").lower()
        if input_type in ("submit", "button"):
            val = locator.get_attribute("value")
            if val:
                return val.strip()
        placeholder = locator.get_attribute("placeholder")
        if placeholder:
            return placeholder.strip()

    # legacy table forms: nearest preceding <td> text in the same row
    try:
        row_text = locator.evaluate(
            """
            (el) => {
                const row = el.closest('tr');
                if (!row) return null;
                const cells = Array.from(row.querySelectorAll('td'));
                const idx = cells.findIndex(td => td.contains(el));
                if (idx > 0) return cells[idx - 1].innerText.trim();
                return null;
            }
            """
        )
        if row_text:
            return row_text
    except Exception:
        pass

    name_attr = locator.get_attribute("name")
    if name_attr:
        return name_attr

    return ""


def _role_of(locator) -> str:
    tag = locator.evaluate("e => e.tagName.toLowerCase()")
    if tag == "a":
        return "link"
    if tag == "select":
        return "combobox"
    if tag == "button":
        return "button"
    if tag == "input":
        input_type = (locator.get_attribute("type") or "text").lower()
        if input_type in ("submit", "button"):
            return "button"
        return "textbox"
    return "generic"


def _target_url_of(page, el, role: str) -> str | None:
    tag = el.evaluate("e => e.tagName.toLowerCase()")

    if tag == "a":
        href = el.get_attribute("href")
        if href:
            return el.evaluate("e => e.href")  # browser-resolved absolute URL
        return None

    if role == "button":
        # a submit button/input inside a <form> navigates to the form's
        # action on click; a plain <button type="button"> does not.
        input_type = (el.get_attribute("type") or "").lower()
        if tag == "button" and input_type not in ("", "submit"):
            return None
        try:
            action = el.evaluate(
                """
                (e) => {
                    const form = e.closest('form');
                    return form ? form.action : null;
                }
                """
            )
            return action
        except Exception:
            return None

    return None


def build_observation(page) -> Observation:
    elements: dict[str, ObservedElement] = {}
    raw_locator = page.locator(_INTERACTIVE_SELECTOR)
    count = raw_locator.count()

    lines: list[str] = []
    for i in range(count):
        el = raw_locator.nth(i)
        if not el.is_visible():
            continue

        role = _role_of(el)
        name = _accessible_name(page, el)
        value = None
        if role == "textbox":
            value = el.input_value()
        elif role == "combobox":
            value = el.input_value()

        ref = f"e{i + 1}"
        candidates = _build_candidates(page, el, role, name)
        target_url = _target_url_of(page, el, role)
        elements[ref] = ObservedElement(
            ref=ref,
            role=role,
            name=name,
            value=value,
            candidates=candidates,
            playwright_locator=el,
            target_url=target_url,
        )

        value_str = f" (current value: '{value}')" if value else ""
        lines.append(f"{ref}: {role} '{name}'{value_str}")

    elements_text = "\n".join(lines) if lines else "(no interactive elements found)"

    # Static/non-interactive page content (table values, labels, messages)
    # matters just as much as interactive controls for a read-heavy goal
    # like "look up member X and read their balance" -- the value being
    # read usually ISN'T an interactive element. Without this, the model
    # would have no way to observe data it isn't allowed to click or type
    # into. Capped to keep prompts bounded; the mock app's pages are small.
    try:
        page_text = page.locator("body").inner_text().strip()
    except Exception:
        page_text = ""
    if len(page_text) > 4000:
        page_text = page_text[:4000] + "\n... (truncated)"

    combined_text = (
        f"INTERACTIVE ELEMENTS (act on these only, by ref):\n{elements_text}\n\n"
        f"VISIBLE PAGE TEXT (read-only, for finding values -- not actionable):\n{page_text}"
    )

    return Observation(
        url=page.url, title=page.title(), text=combined_text, elements=elements, page_text=page_text
    )


def _build_candidates(page, el, role: str, name: str) -> list[LocatorCandidate]:
    candidates: list[LocatorCandidate] = []

    if name:
        try:
            role_locator = page.get_by_role(role, name=name, exact=True)
            if role_locator.count() == 1:
                candidates.append(LocatorCandidate("role_name", f"{role}:{name}"))
        except Exception:
            pass

    name_attr = el.get_attribute("name")
    if name_attr:
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        css = f"{tag}[name='{name_attr}']"
        try:
            if page.locator(css).count() == 1:
                candidates.append(LocatorCandidate("css_name_attr", css))
        except Exception:
            pass

    el_id = el.get_attribute("id")
    if el_id:
        css = f"#{el_id}"
        try:
            if page.locator(css).count() == 1:
                candidates.append(LocatorCandidate("css_id", css))
        except Exception:
            pass

    if name and role in ("link", "button"):
        try:
            text_locator = page.get_by_text(name, exact=True)
            if text_locator.count() == 1:
                candidates.append(LocatorCandidate("text", name))
        except Exception:
            pass

    if not candidates:
        # last-resort positional fallback -- low confidence, logged as such
        candidates.append(LocatorCandidate("positional", "nth-match, no stable attribute found"))

    return candidates
