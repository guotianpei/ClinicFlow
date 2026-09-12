"""Review draft: parser observations only; no production policy validation."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
from enum import Enum
from pathlib import Path


def encode_ast(value):
    if isinstance(value, Enum):
        return {"enum_class": type(value).__name__, "name": value.name, "value": value.value}
    raise TypeError("unsupported AST evidence value")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--serialization-preflight", action="store_true")
    args = ap.parse_args()
    with args.output.open("x") as output:
        result = {
            "scope": "parser observations; policy refusal NOT EXECUTED",
            "python": platform.python_version(),
            "cases": [],
            "passed": False,
        }
        try:
            version = importlib.metadata.version("pglast")
            if version != "7.17":
                raise RuntimeError("pglast version mismatch")
            from pglast import get_postgresql_version, parse_sql
            from pglast.parser import ParseError

            result["pglast"] = version
            result["postgresql_grammar"] = get_postgresql_version()
            cases = json.loads(Path(__file__).with_name("parser-cases.json").read_text())
            if len(cases) != 25 or len({c["id"] for c in cases}) != 25:
                raise ValueError("exact 25 unique parser cases required")
            if args.serialization_preflight:
                cases = [c for c in cases if c["id"] == "key-scope-table"]
            for case in cases:
                row = {
                    "id": case["id"],
                    "expected_parse": case["expected_parse"],
                    "subsequent_category": case["subsequent_category"],
                    "parse_expectation_matches": False,
                }
                result["cases"].append(row)
                try:
                    sql = case["sql"]
                    if "\x00" in sql:
                        raise ValueError("NUL in probe input")
                    row["input_sha256"] = hashlib.sha256(sql.encode()).hexdigest()
                    nodes = parse_sql(sql)
                    row["parsed"] = True
                    row["root_collection"] = type(nodes).__name__
                    row["ast"] = [n(skip_none=False) for n in nodes]
                    row["parameters_collection"] = [
                        type(n.stmt.parameters).__name__
                        for n in nodes
                        if hasattr(n.stmt, "parameters")
                    ]
                    # Serialize before adding this case to accepted evidence.
                    json.dumps(row, default=encode_ast, allow_nan=False)
                    row["parse_expectation_matches"] = case["expected_parse"] is True
                except Exception as exc:
                    row.pop("ast", None)
                    row["exception_type"] = type(exc).__name__
                    row["expected_parse_refusal"] = (
                        isinstance(exc, ParseError) and not case["expected_parse"]
                    )
                    row["parse_expectation_matches"] = row["expected_parse_refusal"]
            result["passed"] = bool(result["cases"]) and all(
                r["parse_expectation_matches"] for r in result["cases"]
            )
            result["serialization_preflight"] = args.serialization_preflight
            payload = json.dumps(
                result, ensure_ascii=False, indent=2, default=encode_ast, allow_nan=False
            )
        except Exception as exc:
            # Construct a minimal serializable failure record before writing any bytes.
            result = {
                "passed": False,
                "failure_type": type(exc).__name__,
                "case_ids_observed": [r["id"] for r in result["cases"]],
            }
            payload = json.dumps(result, indent=2, allow_nan=False)
        output.write(payload + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
