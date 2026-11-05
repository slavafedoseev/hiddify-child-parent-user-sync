#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hiddify-patch-xtlsapi-trojan.py
================================
xtlsapi (3.3.0, upstream main too) builds the trojan account for gRPC AddUser as
    config_pb2.Account(password=..., flow=flow)
but its generated protobuf for trojan (regenerated after xray-core removed trojan XTLS flow)
has NO `flow` field -> ValueError 'Protocol message Account has no "flow" field' on EVERY
trojan inbound. Hiddify's XrayApi.add_client swallows the exception (`except: pass`), so a user
added via panel/API/sync is silently never hot-added to trojan inbounds until xray restarts.

Fix: pass `flow` only when the installed proto actually has that field (version-agnostic).
Idempotent / marker-based. Never exits non-zero (wired as ExecStartPre=+- on hiddify-panel.service).
"""
import glob
import sys

MARKER = "PATCHED-trojan-noflow"
OLD = "account=config_pb2.Account(password=user_id_or_password,flow=flow)"
NEW = ("account=config_pb2.Account(**({'password': user_id_or_password, 'flow': flow} "
       "if 'flow' in config_pb2.Account.DESCRIPTOR.fields_by_name else "
       "{'password': user_id_or_password}))  # " + MARKER)


def main():
    paths = glob.glob("/opt/hiddify-manager/.venv*/lib/python*/site-packages/xtlsapi/api_services/handler/add_client.py") \
        + glob.glob("/opt/hiddify-manager/hiddify-panel/.venv*/lib/python*/site-packages/xtlsapi/api_services/handler/add_client.py")
    if not paths:
        print("[xtlsapi-trojan] add_client.py not found; nothing to do")
        return
    for p in paths:
        try:
            with open(p) as f:
                src = f.read()
            if MARKER in src:
                print(f"[xtlsapi-trojan] already patched: {p}")
                continue
            if OLD not in src:
                print(f"[xtlsapi-trojan] pattern not found (upstream changed?): {p}")
                continue
            with open(p, "w") as f:
                f.write(src.replace(OLD, NEW, 1))
            print(f"[xtlsapi-trojan] patched: {p}")
        except Exception as e:
            print(f"[xtlsapi-trojan] WARN {p}: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[xtlsapi-trojan] WARN: {e}")
    sys.exit(0)
