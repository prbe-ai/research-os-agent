"""Evidence extraction: the ENUMERATE half of a backfill, deepened.

:mod:`probe.cli.backfill` names the split this feature is built on -- ENUMERATE
deterministic, DECIDE agent, RECONCILE deterministic. ENUMERATE used to mean
"count files and bytes", and that is not enough to DECIDE with.

The reason is that project membership is a per-FILE property and not a
per-directory one. A researcher's directory routinely holds pieces of several
lines of work, and one line of work routinely spills across directories nobody
would think to group. Handing an agent a directory tree and asking which folder
is which project is asking it to guess from the one signal that does not carry
the answer.

So this module turns a folder into EVIDENCE, without asking anyone to read a
terabyte:

    Tier 1   every file        path, size, mtime. Free -- the stat is already
                              paid by the census walk. mtime CLUSTERING is the
                              signal that survives a messy tree: files written
                              inside one window are usually one run.

    Tier 2   evidence-bearing  a bounded head sample of the files that can NAME
                              a project -- readme, markdown, config, yaml/json,
                              notebook and script headers, result tables, logs.
                              A few hundred out of tens of thousands.

    Tier 3   the long tail     checkpoints, shards, images, weights. No text in
                              them identifies a project, so they INHERIT from
                              their neighbours, and the rule that placed them is
                              RECORDED per file rather than implied. An
                              inherited assignment a reader cannot audit is
                              indistinguishable from a guess.

Two budgets, both real. Tier 2 opens files, so it is bounded by total bytes read
AND by file count -- an unbounded sample over a network mount is minutes of I/O
before anything uploads. Nothing here reads a file twice.

stdlib only, like the rest of cli/ -- ``probe log`` runs inside training loops
and must not pay for any of this.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .backfill import REFERENCE_ABOVE_BYTES, SKIP_DIRS, human_bytes


class Tier(StrEnum):
    """Which evidence tier a file falls in. See the module docstring."""

    #: Carries text that can name a line of work. Sampled.
    EVIDENCE = "evidence"
    #: Carries no identifying text. Inherits, with the rule recorded.
    TAIL = "tail"


#: Extensions whose CONTENT can name a project. Deliberately a closed list
#: rather than "anything that decodes as text": a 4GB jsonl shard decodes fine
#: and tells you nothing, and sampling it costs the budget a config would have
#: spent better.
EVIDENCE_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".rst", ".txt",
        ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
        ".py", ".sh", ".r", ".jl", ".lua",
        ".ipynb",
        ".csv", ".tsv",
        ".log", ".out", ".err",
        ".tex", ".bib",
    }
)

#: Filenames that are evidence whatever their extension. A README with no
#: suffix is the single most informative file in most research folders.
EVIDENCE_NAMES = frozenset(
    {
        "readme", "notes", "makefile", "dockerfile", "license",
        "requirements", "pyproject", "setup", "environment",
        "config", "params", "hparams", "args", "run", "train",
    }
)

#: Never sampled however they are named -- these decode as text but carry no
#: project identity, and they are big.
TAIL_SUFFIXES = frozenset(
    {
        ".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".h5", ".pkl", ".pickle",
        ".npy", ".npz", ".parquet", ".arrow", ".feather", ".pb", ".onnx",
        ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".svg", ".mp4", ".wav",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
        ".so", ".dylib", ".dll", ".o", ".a",
        ".db", ".sqlite", ".sqlite3", ".wandb",
    }
)

#: Head bytes sampled from ONE evidence file. Enough for a header, a docstring,
#: a config block or a table's columns; short enough that a few hundred of them
#: still fit a classification prompt.
SAMPLE_BYTES = 2048

#: Total bytes Tier 2 may read across the whole folder. At SAMPLE_BYTES each
#: that is ~1500 files, comfortably above the few hundred a real research folder
#: carries and far below what would stall the import.
SAMPLE_BUDGET_BYTES = 3 * 1024 * 1024

#: Ceiling on sampled FILES, independent of bytes. A folder of ten thousand
#: tiny configs would satisfy the byte budget and still blow the prompt.
SAMPLE_BUDGET_FILES = 1500

#: An evidence file at or above this size is sampled but flagged: its head is
#: unlikely to represent it, and a reader should know the sample is a corner of
#: something much larger rather than the whole thing.
LARGE_EVIDENCE_BYTES = 4 * 1024 * 1024

#: Files written within this many seconds of each other are treated as one
#: burst. Research runs write their outputs together; the gaps between runs are
#: minutes to days, so this separates cleanly without tuning per folder.
CLUSTER_GAP_SECONDS = 15 * 60


@dataclass(frozen=True)
class FileEvidence:
    """One file's Tier 1 facts, plus its Tier 2 sample when it has one."""

    path: str
    size: int
    mtime: float
    tier: Tier
    sample: str | None = None
    #: Set when the sample is a head of something much larger than the sample.
    truncated: bool = False
    #: Why a TAIL file was placed where it was. Set by the caller that resolves
    #: inheritance, never guessed here -- an unaudited inheritance is a guess
    #: wearing a rule's clothes.
    inherited_from: str | None = None


@dataclass
class Cluster:
    """A burst of files written together. The same-run signal."""

    started: float
    ended: float
    paths: list[str] = field(default_factory=list)

    @property
    def span_seconds(self) -> float:
        return max(0.0, self.ended - self.started)


@dataclass
class Evidence:
    """Everything ENUMERATE produces for one folder."""

    root: str
    files: list[FileEvidence]
    clusters: list[Cluster]
    sampled_files: int
    sampled_bytes: int
    #: True when a budget stopped the sampling early. The classification prompt
    #: MUST say so: an agent told it saw everything, when it saw a prefix, will
    #: report confidence it has not earned.
    sample_budget_hit: bool = False

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def describe(self) -> str:
        ev = sum(1 for f in self.files if f.tier is Tier.EVIDENCE)
        return (
            f"{self.total_files:,} files   {human_bytes(self.total_bytes)}   "
            f"{ev:,} evidence-bearing, {self.sampled_files:,} sampled"
            + ("  (sample budget reached)" if self.sample_budget_hit else "")
        )


def _stem_key(name: str) -> str:
    """The lowercased stem, for matching against EVIDENCE_NAMES."""
    return name.rsplit(".", 1)[0].lower() if "." in name else name.lower()


def tier_for(name: str, size: int) -> Tier:
    """Which tier a file belongs to, from its NAME and SIZE alone.

    Name and size only, deliberately: this runs for every file in the tree, so
    it may not open anything. Size participates because a 10GB ``.json`` is a
    dataset shard whatever its extension claims, and sampling its head would
    spend the budget describing a bracket.
    """
    suffix = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    if suffix in TAIL_SUFFIXES:
        return Tier.TAIL
    if size >= REFERENCE_ABOVE_BYTES:
        # Uploaded by reference anyway; its bytes are never read for content.
        return Tier.TAIL
    if suffix in EVIDENCE_SUFFIXES or _stem_key(name) in EVIDENCE_NAMES:
        return Tier.EVIDENCE
    return Tier.TAIL


def walk(root: Path) -> list[FileEvidence]:
    """Tier 1 for every file under `root`, pruning SKIP_DIRS.

    One stat per file and no reads, so this stays affordable on a network mount.
    Shares :data:`probe.cli.backfill.SKIP_DIRS` rather than restating it: a file
    the census does not count must not become evidence, or the denominator and
    the classification would disagree about what the folder even contains.
    """
    out: list[FileEvidence] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                # A broken symlink, or a race with a job deleting a checkpoint.
                # It still counts -- the census counts it too, and an evidence
                # list that quietly skips it would not reconcile.
                size, mtime = 0, 0.0
            out.append(
                FileEvidence(
                    path=str(full),
                    size=size,
                    mtime=mtime,
                    tier=tier_for(name, size),
                )
            )
    return out


def cluster_by_mtime(
    files: list[FileEvidence], *, gap_seconds: float = CLUSTER_GAP_SECONDS
) -> list[Cluster]:
    """Group files into write bursts.

    The one grouping signal that does not care how the tree is laid out. Files
    with no usable mtime (the OSError path in :func:`walk`) are left out
    entirely rather than pooled into a fictional cluster at the epoch.
    """
    dated = sorted((f for f in files if f.mtime > 0), key=lambda f: f.mtime)
    clusters: list[Cluster] = []
    for f in dated:
        if clusters and f.mtime - clusters[-1].ended <= gap_seconds:
            clusters[-1].ended = f.mtime
            clusters[-1].paths.append(f.path)
        else:
            clusters.append(Cluster(started=f.mtime, ended=f.mtime, paths=[f.path]))
    return clusters


def read_head(path: str, *, limit: int = SAMPLE_BYTES) -> str | None:
    """A text head of `path`, or None when it is not usefully text.

    Decoded with ``errors="replace"`` rather than skipped on a decode error: a
    config with one stray byte is still a config, and losing it because of that
    byte is worse than a replacement character in a prompt. Binary is rejected
    by NUL sniffing instead, which is cheap and does not depend on the suffix
    list being complete.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit)
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    return text or None


def sample(
    files: list[FileEvidence],
    *,
    budget_bytes: int = SAMPLE_BUDGET_BYTES,
    budget_files: int = SAMPLE_BUDGET_FILES,
) -> tuple[list[FileEvidence], int, int, bool]:
    """Tier 2. Returns ``(files, sampled_count, sampled_bytes, budget_hit)``.

    Evidence files are sampled SMALLEST FIRST. That is not arbitrary: a folder's
    identity lives in its readmes and configs, which are small, while its big
    "evidence" files are usually logs whose head is boilerplate. Smallest-first
    spends a fixed budget on the most files, and the ones it drops are the ones
    that would have said least.
    """
    order = sorted(
        (i for i, f in enumerate(files) if f.tier is Tier.EVIDENCE),
        key=lambda i: files[i].size,
    )
    out = list(files)
    used_bytes = 0
    used_files = 0
    budget_hit = False
    for i in order:
        if used_files >= budget_files or used_bytes >= budget_bytes:
            budget_hit = True
            break
        f = files[i]
        head = read_head(f.path)
        if head is None:
            # Reads as binary despite its name. Reclassify rather than pretend:
            # the classifier must not be handed replacement characters as if
            # they meant something.
            out[i] = FileEvidence(
                path=f.path, size=f.size, mtime=f.mtime, tier=Tier.TAIL
            )
            continue
        used_bytes += len(head.encode("utf-8", errors="ignore"))
        used_files += 1
        out[i] = FileEvidence(
            path=f.path,
            size=f.size,
            mtime=f.mtime,
            tier=Tier.EVIDENCE,
            sample=head,
            truncated=f.size > LARGE_EVIDENCE_BYTES or f.size > len(head),
        )
    return out, used_files, used_bytes, budget_hit


def gather(root: Path) -> Evidence:
    """Walk, cluster and sample `root`. The whole of ENUMERATE, in order."""
    root = Path(root).resolve()
    files = walk(root)
    files, n_files, n_bytes, hit = sample(files)
    return Evidence(
        root=str(root),
        files=files,
        clusters=cluster_by_mtime(files),
        sampled_files=n_files,
        sampled_bytes=n_bytes,
        sample_budget_hit=hit,
    )


def to_jsonl(evidence: Evidence) -> str:
    """Evidence as JSONL, one file per line, for the classification prompt.

    Paths are RELATIVE to the root. The agent's whole job is deciding what
    belongs together; absolute paths add a constant prefix to every line, which
    costs prompt budget and tells it nothing.
    """
    root = Path(evidence.root)
    lines: list[str] = []
    for f in evidence.files:
        try:
            rel = str(Path(f.path).relative_to(root))
        except ValueError:  # pragma: no cover - walk never leaves the root
            rel = f.path
        row: dict[str, object] = {"path": rel, "size": f.size, "tier": f.tier.value}
        if f.mtime:
            row["mtime"] = int(f.mtime)
        if f.sample:
            row["sample"] = f.sample
            if f.truncated:
                row["sample_truncated"] = True
        lines.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(lines)
