#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Structural stats of BSP files (bsplib.Bsp.stats).

  python -I tools/bspinfo.py <map.bsp>                 # one map: lump table, BSPX, stats
  python -I tools/bspinfo.py <dirs/files...> -o x.tsv   # many: one TSV row each + a summary

Columns: path, format, bytes, cm_bytes (the lumps cmodel.c reads), planes, nodes,
clipnodes, leafs, visleafs, vis, models, entities, bspx (lump names), flags, sha256.
flags: lump_outside_file, unaligned, v29_unsigned_children (> 32767 nodes or
clipnodes in a 16-bit format), novis, visleafs_gt_32767, no_clipnodes, bspx_invalid.
"""
import argparse, collections, hashlib, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir
from bsplib import Bsp, CMODEL_LUMPS, LUMP_NAMES, struct_size

COLS = ["path", "format", "bytes", "cm_bytes", "planes", "nodes", "clipnodes", "leafs", "visleafs", "vis",
        "models", "entities", "bspx", "flags", "sha256"]


def collect(paths):
	out = []
	for p in paths:
		if os.path.isdir(p):
			for root, _, files in os.walk(p):
				out += [os.path.join(root, f) for f in files if f.lower().endswith(".bsp")]
		else:
			out.append(p)
	return sorted(out)


def show_one(path):
	data = open(path, "rb").read()
	b = Bsp(data)
	print("%s: %s, %d bytes, sha256 %s" % (path, b.fmt or "not a BSP", len(data), hashlib.sha256(data).hexdigest()))
	if not b.ok:
		return
	print("  %-2s %-12s %10s %10s %8s  %s" % ("#", "lump", "fileofs", "filelen", "records", ""))
	for i, (o, l) in enumerate(b.lumps):
		size = struct_size(b.fmt, i)
		recs = "%d%s" % (l // size, "+%d" % (l % size) if l % size else "") if size else ""
		note = ("cmodel " if i in CMODEL_LUMPS else "") + ("" if b.lump_in_file(i) else "OUTSIDE FILE")
		print("  %-2d %-12s %10d %10d %8s  %s" % (i, LUMP_NAMES[i], o, l, recs, note))
	if b.bspx is not None:
		print("  BSPX at %d%s:" % (b.bspx_offset, "" if b.bspx_valid else " (rejected by CM_LoadBSPX)"))
		for n, o, l in b.bspx:
			print("     %-24s %10d %10d" % (n.decode("latin1"), o, l))
	for k, v in b.stats().items():
		if k not in ("format", "bytes", "bspx"):
			print("  %-9s %s" % (k, "" if v is None else v))


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("paths", nargs="+")
	ap.add_argument("-o", "--out", help="TSV output (default: stdout)")
	ap.add_argument("--root", help="write paths relative to this directory")
	a = ap.parse_args()

	files = collect(a.paths)
	if len(files) == 1 and not a.out:
		return show_one(files[0])

	out = open(a.out, "w", encoding="utf-8", newline="\n") if a.out else sys.stdout
	out.write("\t".join(COLS) + "\n")
	fmts, flags = collections.Counter(), collections.Counter()
	for f in files:
		data = open(f, "rb").read()
		s = Bsp(data).stats()
		s["path"] = (os.path.relpath(f, a.root) if a.root else f).replace(os.sep, "/")
		s["sha256"] = hashlib.sha256(data).hexdigest()
		out.write("\t".join("" if s.get(c) is None else str(s[c]) for c in COLS) + "\n")
		fmts[s["format"]] += 1
		flags.update(x for x in s.get("flags", "").split(",") if x)
	if a.out:
		out.close()
		print("%d maps: %s" % (len(files), ", ".join("%s %d" % kv for kv in fmts.most_common())))
		print("flags: " + ", ".join("%s %d" % kv for kv in flags.most_common()))


if __name__ == "__main__":
	main()
