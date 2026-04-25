#!/usr/bin/env python3
"""Ensure repo-root ``.env`` has a strong random ``POSTGRES_PASSWORD``.

Docker Compose needs this variable **before** Postgres starts, so it cannot be
generated from the FreeAI app lifespan. Run once (or any time the password is
still a placeholder) before ``docker compose up``:

    python scripts/ensure_dotenv.py

If ``.env`` is missing, it is created from ``backend/.env.example`` when present,
then the Postgres password line is set or replaced with a new secret.
"""
from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOTENV = REPO_ROOT / ".env"
EXAMPLE = REPO_ROOT / "backend" / ".env.example"

# Values we treat as "not yet configured" and replace.
_PLACEHOLDER_PASSWORDS = frozenset(
    {
        "",
        "change-me",
        "change-me-use-openssl-rand-base64-32",
    }
)


def _generate_postgres_password() -> str:
    # url-safe, no shell/.env quoting issues for typical compose usage
    return secrets.token_urlsafe(32)


def _line_sets_postgres_password(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#") and s.startswith("POSTGRES_PASSWORD=")


def _value_from_postgres_line(line: str) -> str:
    _, _, rest = line.partition("=")
    return rest.strip().strip('"').strip("'").rstrip("\r")


def _ensure_dotenv_file_exists() -> None:
    if DOTENV.exists():
        return
    DOTENV.parent.mkdir(parents=True, exist_ok=True)
    if EXAMPLE.is_file():
        shutil.copyfile(EXAMPLE, DOTENV)
        print(f"Created {DOTENV} from {EXAMPLE}", file=sys.stderr)
    else:
        DOTENV.write_text(
            "# FreeAI local environment — see backend/.env.example\n",
            encoding="utf-8",
        )
        print(f"Created minimal {DOTENV} (no {EXAMPLE} found)", file=sys.stderr)


def ensure_postgres_password(*, force: bool = False) -> str:
    """Write ``POSTGRES_PASSWORD`` into ``.env`` if missing or placeholder.

    Returns the password value that is now configured (existing or new).
    """
    _ensure_dotenv_file_exists()
    text = DOTENV.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [""]

    out: list[str] = []
    found = False
    current_password: str | None = None
    replaced = False

    for line in lines:
        if _line_sets_postgres_password(line):
            found = True
            val = _value_from_postgres_line(line)
            current_password = val
            if force or val in _PLACEHOLDER_PASSWORDS:
                pw = _generate_postgres_password()
                newline = "\n" if line.endswith("\n") else ""
                out.append(f"POSTGRES_PASSWORD={pw}{newline}")
                current_password = pw
                replaced = True
            else:
                out.append(line)
        else:
            out.append(line)

    if not found:
        pw = _generate_postgres_password()
        if out and out[-1].strip():
            out.append("\n")
        out.append(f"POSTGRES_PASSWORD={pw}\n")
        current_password = pw
        replaced = True

    if replaced:
        tmp = DOTENV.with_suffix(".env.tmp")
        tmp.write_text("".join(out), encoding="utf-8")
        tmp.replace(DOTENV)
        print(
            "POSTGRES_PASSWORD set to a new random value in .env "
            "(keep this file private; it is gitignored).",
            file=sys.stderr,
        )
    else:
        print("POSTGRES_PASSWORD already set to a non-placeholder value; left unchanged.", file=sys.stderr)

    assert current_password is not None
    return current_password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace POSTGRES_PASSWORD even if it does not look like a placeholder",
    )
    args = parser.parse_args()
    try:
        ensure_postgres_password(force=args.force)
    except OSError as e:
        print(f"error: could not write {DOTENV}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
