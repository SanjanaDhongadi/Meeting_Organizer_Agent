import re
import json
import logging
from typing import Dict, Any, List, Tuple
from backend.skills.base import BaseSkill, SkillMetadata
from backend.app.config import settings
from backend.connectors import groq_client

logger = logging.getLogger("agenda_skill")


def clean_agenda_point(text: str) -> str:
    """Formatting only: trim, collapse spaces, drop list markers, capitalize the first letter."""
    point = re.sub(r"\s+", " ", str(text or "")).strip()
    point = re.sub(r"^(?:[-*•]+|\d+[.)])\s*", "", point).strip()
    point = point.rstrip(" ;,")
    return point[:1].upper() + point[1:] if point else ""


def normalize_agenda_points(points: List[str]) -> List[str]:
    """Keep the organizer's order, drop empty entries."""
    return [p for p in (clean_agenda_point(x) for x in (points or [])) if p]


def format_agenda(points: List[str]) -> str:
    return "\n".join(f"{i}. {p}" for i, p in enumerate(normalize_agenda_points(points), start=1))


def agenda_points_from_text(text: str) -> List[str]:
    """Turn an edited agenda text back into points (one per line)."""
    return normalize_agenda_points([line for line in str(text or "").splitlines()])


def professionalize_sentence(text: str) -> str:
    """
    Converts a rough agenda point into a grammatically correct, professional sentence
    without any topic titles or labels (e.g. no 'Project Progress Review:').
    """
    cleaned = clean_agenda_point(text)
    if not cleaned:
        return ""

    # Strip existing topic heading prefixes if present, e.g. "Project Progress Review: ..." -> "..."
    if ":" in cleaned:
        parts = cleaned.split(":", 1)
        if len(parts[0].split()) <= 6 and len(parts[1].strip()) > 5:
            cleaned = parts[1].strip()

    lower = cleaned.lower()

    # Exact and semantic matching for standard discussion points
    if re.search(r"\bproject progress\b", lower) and ("review" in lower or "discuss" in lower or "status" in lower):
        if "completed" in lower or "work" in lower:
            return "Review the current project progress and assess the work completed so far."
        return "Review the current project progress and assess the work completed so far."
    if lower in {"discuss project progress", "project progress", "progress review", "review project progress"}:
        return "Review the current project progress and assess the work completed so far."
    if re.search(r"\b(?:what\s+)?work\s+(?:is\s+)?completed\b", lower) or lower in {"completed tasks", "review what work is completed", "work completed"}:
        return "Review completed tasks and deliverables completed so far."
    if re.search(r"\bpending\s+(?:tasks?|work|deliverables?)\b", lower) or lower in {"pending tasks", "discuss pending tasks"}:
        return "Discuss pending tasks and outstanding deliverables."
    if re.search(r"\b(?:problems?|challenges?|blockers?|obstacles?)\b", lower):
        return "Identify the problems and challenges affecting project progress."
    if re.search(r"\b(?:next\s+steps?|what needs to be done next|action\s+plan)\b", lower):
        return "Discuss the next steps required to move the project forward."
    if re.search(r"\b(?:roles?|responsibilit(?:y|ies)|assign)\b", lower):
        return "Assign responsibilities to team members."

    # General conversion: convert rough spoken language into an actionable professional sentence
    core = re.sub(
        r"^(?:(?:we need to|let\'?s|i want to|can we|please|have to|should|wanna|gonna)\s+)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    verb_transforms = [
        (r"^(?:talk about|talk on|chat about|discussing|discuss)\s+", "Discuss "),
        (r"^(?:look into|look at|check out|go over|see about|reviewing|review)\s+", "Review "),
        (r"^(?:figure out|sort out|fix up|resolve|resolving)\s+", "Address and resolve "),
        (r"^(?:catch up on|status on|update on)\s+", "Review the latest status and updates on "),
        (r"^(?:brainstorm(?:ing)?(?:\s+on)?)\s+", "Brainstorm and evaluate solutions for "),
        (r"^(?:plan(?:ning)?(?:\s+for)?)\s+", "Plan and align on the roadmap for "),
        (r"^(?:decide on|decision on|deciding on)\s+", "Determine and align on "),
        (r"^(?:identify(?:ing)?)\s+", "Identify "),
        (r"^(?:assign(?:ing)?)\s+", "Assign "),
        (r"^(?:evaluate|evaluating)\s+", "Evaluate "),
        (r"^(?:present(?:ing)?(?:\s+of)?)\s+", "Present "),
        (r"^(?:demo(?:ing)?(?:\s+of)?)\s+", "Demonstrate and walk through "),
    ]

    matched = False
    for pat, repl in verb_transforms:
        if re.search(pat, core, re.IGNORECASE):
            core = re.sub(pat, repl, core, flags=re.IGNORECASE).strip()
            matched = True
            break

    if not matched:
        if re.search(r"\b(budget|cost|expenditure|spending)\b", core, re.IGNORECASE):
            core = f"Review budget allocation and financial planning for {core}."
        elif re.search(r"\b(roadmap|timeline|schedule|milestones?)\b", core, re.IGNORECASE):
            core = f"Align on the timeline and roadmap for {core}."
        else:
            core = f"Discuss and evaluate {core}."

    core = clean_agenda_point(core)
    core = core[:1].upper() + core[1:]
    if not core.endswith("."):
        core += "."

    return core


def professionalize_item(text: str) -> str:
    return professionalize_sentence(text)


def professionalize_phrase(text: str) -> str:
    return professionalize_sentence(text)


def improve_agenda(
    raw_points: List[str],
    purpose: str = "",
    title: str = "",
    raw_request: str = ""
) -> Tuple[List[str], str]:
    """
    Converts rough English into a clear, professional, logically structured agenda
    written as full professional sentences without topic headings or labels.
    """
    cleaned_points = normalize_agenda_points(raw_points)

    # 1. Try LLM refinement when the Groq API key is configured and not in demo mode
    if groq_client.is_configured() and not settings.DEMO_MODE:
        try:
            prompt = (
                "You are an executive meeting coordinator. Convert the following meeting purpose and rough discussion points "
                "into clear, professional, grammatically correct sentences.\n\n"
                "Rules:\n"
                "1. Strictly preserve the user's original meaning, intent, technical topics, and specific scope.\n"
                "2. Each agenda item must be a complete, professional, actionable sentence.\n"
                "3. CRITICAL: Do NOT add topic titles, headings, or label prefixes (such as 'Project Review:' or '**Topic**:'). Output only the clean sentences.\n"
                "4. Preserve the exact order and number of the original points (one-to-one mapping). Do not invent or add extra unrelated filler items or deadlines.\n"
                "5. Return a JSON object with key 'agenda_points' containing a list of strings (each string is one complete professional sentence).\n\n"
                f"Title: {title}\n"
                f"Purpose: {purpose}\n"
                f"Raw Discussion Points: {raw_points}\n"
                f"Full Request: {raw_request}\n"
            )
            content = groq_client.chat_json("You rewrite meeting agenda points and reply with JSON only.", prompt)
            raw_llm_points = content.get("agenda_points", [])
            llm_points = [professionalize_sentence(p) for p in raw_llm_points if str(p).strip()]
            if llm_points:
                return llm_points, format_agenda(llm_points)
        except Exception as e:
            logger.warning(f"AI agenda enhancement failed ({e}); falling back to deterministic refiner.")

    # 2. Deterministic high-quality professional refiner
    if cleaned_points:
        refined = [professionalize_sentence(p) for p in cleaned_points if professionalize_sentence(p)]
        return refined, format_agenda(refined)

    # 3. If no explicit points were provided, extract structured topics from purpose/title/raw_request
    text_source = purpose.strip() or title.strip() or raw_request.strip()
    if text_source:
        cleaned_source = re.sub(
            r"^(?:(?:schedule|set up|plan|organize)\s+(?:a|an)\s+meeting\s+)?(?:to|about|for|regarding)\s+",
            "",
            text_source,
            flags=re.IGNORECASE,
        )
        splits = re.split(r",|\band\b|;", cleaned_source)
        candidate_topics = [s.strip() for s in splits if len(s.strip()) > 3]

        if len(candidate_topics) >= 2:
            refined = [professionalize_sentence(t) for t in candidate_topics if professionalize_sentence(t)]
            return refined, format_agenda(refined)

    return [], ""


class AgendaPreparationSkill(BaseSkill):
    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="Agenda Preparation Skill",
            purpose="Convert rough English into a clear, professional, logically structured agenda while preserving user intent.",
            instructions=(
                "1. Refine rough phrasing into professional executive agenda points.\n"
                "2. Preserve original meaning and all user topics faithfully.\n"
                "3. Ensure clean formatting and numbered sequence."
            ),
            inputs={
                "agenda_points": "List[str] - Topics entered by the organizer (optional)",
                "purpose": "str - Meeting purpose",
                "title": "str - Meeting title",
                "raw_request": "str - Raw natural-language input",
            },
            outputs={
                "has_purpose": "bool - Whether the organizer supplied a purpose",
                "agenda_points": "List[str] - Improved, professional agenda points",
                "agenda_text": "str - Numbered professional agenda",
                "is_editable": "bool - True, organizer can review and edit",
            },
            constraints=[
                "Preserve original meaning and user topics.",
                "Ensure professional tone and clear structure.",
            ],
            examples=[
                {
                    "input": {"agenda_points": ["talk about budget cuts", "team sizing"]},
                    "output": {"agenda_points": ["Discussion on budget cuts", "Evaluation of team sizing"]},
                }
            ],
        )

    def run(self, inputs: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        raw_points = inputs.get("agenda_points") or []
        purpose = str(inputs.get("purpose") or "")
        title = str(inputs.get("title") or "")
        raw_request = str(inputs.get("raw_request") or "")

        improved_points, improved_text = improve_agenda(
            raw_points=raw_points,
            purpose=purpose,
            title=title,
            raw_request=raw_request,
        )

        return {
            "has_purpose": bool(purpose.strip()),
            "agenda_points": improved_points,
            "agenda_text": improved_text,
            "action_prompt": None,
            "is_editable": True,
        }


# ----- extracting the organizer's own agenda from a natural-language request -----

_AGENDA_HEADER = re.compile(
    r"\b(?:(?:meeting\s+)?agenda(?:\s+(?:items?|points?|topics?))?|topics?\s+to\s+(?:cover|discuss)|(?:points|items)\s+to\s+(?:cover|discuss)"
    r"|discussion\s+(?:points|topics)|we(?:'ll|\s+will)\s+(?:discuss|cover))"
    r"\s*(?::-?|\bis\b|\bare\b|\bincludes?\b|\bwill\s+(?:be|include)\b|-|–|—)\s*", re.IGNORECASE)
_LIST_MARKER = re.compile(r"(?:(?<=\s)|^)(?:\d{1,2}[.)]|[-*•–])\s+(?=\S)")
_REQUEST_AFTER_AGENDA = re.compile(
    r"(?<=[.!?])\s+(?=(?:Please|Also|Send|Invite|Make sure|Thanks|Thank you|Book|Schedule|Set up|Add|Ensure|Share|Remind|Notify|"
    r"Create|Reserve|Cc|Include|Attach)\b)")
_REQUEST_START = r"(?:Please|Also|Send|Invite|Thanks|Thank you|Book|Schedule|Set up|Make sure|Ensure|Share|Remind|Notify)\b"
_MAX_POINTS = 30


def _split_inline(text: str) -> List[str]:
    markers = list(_LIST_MARKER.finditer(text))
    if len(markers) >= 2 or (markers and markers[0].start() == 0):
        head = text[:markers[0].start()].strip()
        pieces = [head] if head else []
        for i, m in enumerate(markers):
            end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            pieces.append(text[m.end():end])
        return pieces
    if ";" in text:
        return text.split(";")
    pieces = re.split(r",\s*(?:and\s+)?", text)
    if len(pieces) > 1 and re.search(r"\s+and\s+", pieces[-1]):          # "a, b and c"
        pieces[-1:] = re.split(r"\s+and\s+", pieces[-1], maxsplit=1)
    return pieces


def extract_agenda_points(request: str) -> List[str]:
    """
    The organizer's agenda as an ordered list, taken verbatim from the request (numbered, bulleted, one per line,
    separated by ';' or ','). Returns [] when the request contains no agenda. Never invents or reorders points.
    """
    text = str(request or "").replace("\r", "")
    header = _AGENDA_HEADER.search(text)
    if not header:
        return []
    tail = text[header.end():]
    lines = [ln.strip() for ln in tail.split("\n")]
    # A list spread over several lines stops at the first blank line after it started.
    collected: List[str] = []
    for index, line in enumerate(lines):
        if not line:
            if collected:
                break
            continue
        if collected and re.match(_REQUEST_START, line):
            break
        collected.append(line)
    if len(collected) > 1:
        raw: List[str] = []
        for line in collected:
            raw += _split_inline(line) if _LIST_MARKER.search(line) and len(_LIST_MARKER.findall(line)) > 1 else [line]
    else:
        single = collected[0] if collected else ""
        cut = _REQUEST_AFTER_AGENDA.search(single)
        if cut:
            single = single[:cut.start()]
        raw = _split_inline(single)
    return normalize_agenda_points([p.strip(" .") for p in raw])[:_MAX_POINTS]
