#!/usr/bin/env python3
"""Check search quality against the real docs library ($LIFERAY_DOCS_DIR, else ~/.liferay-docs).

Not part of the test suite: it needs a built library. Each case is a query and
the title fragments an acceptable hit must contain; the score is how high the
first acceptable hit ranks. Re-run it after changing the index, the ranking
weights in skills/liferay-expert/scripts/docs.py, or the corpus (e.g. after a
full community refetch), and add a case when a real question was answered badly.

    uv run python evals/search_eval.py            # summary + misses
    uv run python evals/search_eval.py -v         # every case
"""

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("docs", ROOT / "skills" / "liferay-expert" / "scripts" / "docs.py")
docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs)

# (search terms, title fragments of an acceptable hit)
CASES = [
    (["client", "extension"], ["client extension"]),
    (["elasticsearch", "connection"], ["elasticsearch"]),
    (["synonym", "sets"], ["synonym"]),
    (["search", "blueprint"], ["blueprint"]),
    (["oauth2", "headless"], ["oauth"]),
    (["company.security.auth.type"], ["portal properties"]),
    (["service.xml"], ["service builder"]),
    (["rest-config.yaml"], ["rest builder"]),
    (["mcp", "server"], ["mcp"]),
    (["breaking", "changes", "2025"], ["breaking changes"]),
    (["object", "definition"], ["object"]),
    (["saml"], ["saml"]),
    (["fragment", "freemarker"], ["fragment"]),
    (["workflow", "approval"], ["workflow"]),
    (["docker", "quick", "start"], ["docker"]),
    (["helm", "kubernetes"], ["kubernetes", "helm"]),
    (["osgi", "configuration", "file"], ["configuration"]),
    (["commerce", "catalog"], ["catalog"]),
    (["virtual", "instance"], ["virtual instance"]),
    (["jvm", "heap"], ["jvm"]),
    (["ClassNotFoundException"], ["classnotfound"]),
    (["reindex"], ["reindex"]),
    (["structure", "template"], ["template", "structure"]),
    (["permission", "role"], ["permission", "role"]),
    (["session", "timeout"], ["session"]),
    (["elasticsearch", "sidecar"], ["sidecar", "elasticsearch"]),
    (["headless", "api", "authentication"], ["authenti", "oauth", "headless"]),
    (["theme", "css"], ["theme", "css"]),
    (["email", "notification", "template"], ["email", "notification"]),
    (["ldap", "import"], ["ldap"]),
]
DEPTH = 8


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="Print every case, not just misses.")
    args = parser.parse_args()

    connection, reason = docs.open_db(docs.docs_dir(None))
    if connection is None:
        sys.exit(f"Full-text search unavailable: {reason}")
    reciprocal = top1 = top3 = 0
    for terms, needles in CASES:
        options = argparse.Namespace(terms=terms, source=None, capability=None, since=None, limit=DEPTH)
        rows, _ = docs.full_text_hits(connection, options, " ")
        if not rows and len(terms) > 1:
            rows, _ = docs.full_text_hits(connection, options, " OR ")
        rank = next((i for i, row in enumerate(rows, 1) if any(n in row[1].lower() for n in needles)), None)
        reciprocal += 1 / rank if rank else 0
        top1 += rank == 1
        top3 += bool(rank and rank <= 3)
        if args.verbose or rank != 1:
            top = rows[0][1][:50] if rows else "-"
            print(f"rank={rank or '-':>1}  {' '.join(terms):<38} top: {top}")
    n = len(CASES)
    print(f"\ncases={n}  MRR@{DEPTH}={reciprocal / n:.3f}  top1={top1}/{n}  top3={top3}/{n}")


if __name__ == "__main__":
    main()
