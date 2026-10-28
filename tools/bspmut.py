#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Structure-aware BSP mutations, deterministic mode: the synthetic regression
cases. Every case is one known defect or boundary, planted in a synthetic
base map (tools/bspgen.py) of each format it applies to, together with what
the *fixed* loader must do with it.

  python tools/bspmut.py cases [outdir]          write the cases + expected.tsv
                                                 (default cases/synthetic)
  python tools/bspmut.py check <exe> [opts]      run a harness build over them
        --cases DIR   (default cases/synthetic)  -c  client path
        -j N  -t SECS  -o results.tsv  -v (print every case, not only failures)
  python tools/bspmut.py table <results.tsv>...  cases x builds, pass/fail
  python tools/bspmut.py seeds [outdir]          small synthetic fuzz seeds (default
                                                 fuzz/seeds_synth): every format, BSPX

Expectations: master's outcome where it is safe,
otherwise the fix's. An expectation is "OUTCOME[|OUTCOME...][~regex]": the
result must be one of the outcomes, and the regex (if any) must match its
message (notes, for OK). One expectation covers both the server and the
client path. A "policy" in the why column means master's outcome is kept
although a stricter loader could do better.
"""
import argparse, csv, hashlib, os, re, struct, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bspgen import Map, leaf, FORMATS, CONTENTS_EMPTY, CONTENTS_SOLID
from bsplib import (HEADER_SIZE, LUMP_ENTITIES, LUMP_PLANES, LUMP_TEXTURES, LUMP_VISIBILITY, LUMP_NODES,
                    LUMP_CLIPNODES, LUMP_LEAFS, LUMP_MODELS, LUMP_NAMES)

F16 = ("v29", "hl")          # 16-bit children
F32 = ("bsp2", "2psb")       # 32-bit children

# what the loader under test may reject with; any message
REJECT = "HOST_ERROR"
# loads, and the physics normals are the clipnode planes' (no BSPX/.qpn source used)
NO_NORMALS = "OK~^(?!.*normals=)"


class Case:
	def __init__(self, name, family, data, expect, why, extra=None):
		self.name, self.family, self.data, self.expect, self.why = name, family, data, expect, why
		self.extra = extra or {}   # suffix -> bytes, written next to the map (".qpn")


def base(fmt, **kw):
	return Map.room(fmt, **kw)


def put(data, fmt, at, *v):
	d = bytearray(data)
	struct.pack_into(fmt, d, at, *v)
	return bytes(d)


def lump_entry(i):
	"""offset of lump i's (fileofs, filelen) in the header"""
	return 4 + 8 * i


def set_lump(data, i, ofs=None, length=None):
	o, l = struct.unpack_from("<ii", data, lump_entry(i))
	return put(data, "<ii", lump_entry(i), o if ofs is None else ofs, l if length is None else length)


def permute_nodes(m, order):
	"""reorder m.nodes: new index k holds old node order[k]"""
	new_of = {old: k for k, old in enumerate(order)}
	m.nodes = [m.nodes[o] for o in order]
	for n in m.nodes:
		for j in (1, 2):
			if n[j] >= 0:
				n[j] = new_of[n[j]]
	for mdl in m.models:
		mdl[3][0] = new_of[mdl[3][0]]


def permute_clipnodes(m, order):
	new_of = {old: k for k, old in enumerate(order)}
	m.clipnodes = [m.clipnodes[o] for o in order]
	for n in m.clipnodes:
		for j in (1, 2):
			if n[j] >= 0:
				n[j] = new_of[n[j]]
	nh = 4 if m.halflife else 3
	for mdl in m.models:
		for h in range(1, nh):
			if mdl[3][h] in new_of:
				mdl[3][h] = new_of[mdl[3][h]]


def normals_blob(m, flip=False):
	"""an MVDSV_PHYSICSNORMALS lump: rampjump, maxgroundspeed, one normal per
	clipnode. Distinct from the plane normals, so the oracle can tell them apart."""
	out = bytearray(struct.pack("<ff", 1.0, 320.0))
	for i, (p, _, _) in enumerate(m.clipnodes):
		n = m.planes[p][:3]
		n = [-x for x in n] if flip else [n[1], n[2], n[0]]
		out += struct.pack("<3fi", *n, 2 if i & 1 else 0)
	return bytes(out)


def bspx_dir(entries, numlumps=None, magic=b"BSPX"):
	"""raw BSPX directory + data for appending at a known (aligned) offset;
	entries: [(name, data)] placed after the directory. Returns a function of
	the directory's file offset, since entries hold absolute offsets."""
	def at(ofs):
		n = len(entries) if numlumps is None else numlumps
		head = bytearray(magic + struct.pack("<i", n))
		body = bytearray()
		pos = ofs + 8 + 32 * len(entries)
		for name, data in entries:
			head += struct.pack("<24sii", name, pos + len(body), len(data))
			body += data + bytes(-len(data) % 4)
		return bytes(head + body)
	return at


def with_trailer(m, make):
	"""serialize m, then append make(offset) at the 4-aligned end of the lumps"""
	d = m.serialize()
	d += bytes(-len(d) % 4)
	return d + make(len(d))


# ------------------------------------------------------------------ families

def fam_base():
	for f in FORMATS:
		yield Case("base_" + f, "base", base(f).serialize(), "OK", "the synthetic base map")
		yield Case("base_novis_" + f, "base", base(f, vis="none").serialize(), "OK", "no vis lump: all visible")
		m = base(f)
		m.leafs[3][1] = -1
		yield Case("base_visofs-1_" + f, "base", m.serialize(), "OK", "a leaf with visofs -1 (no vis row): all visible")
		m = base(f, cells=1, subs=0)
		yield Case("base_1cell_" + f, "base", m.serialize(), "OK", "one leaf, no submodels")
		m = base(f, subs=3)
		m.place = [(i, 1) for i in range(15)]
		yield Case("unaligned_" + f, "layout", m.serialize(), "OK", "every lump at offset = 1 mod 4 (real maps: 304)")


def fam_bspx():
	for f in FORMATS:
		m = base(f)
		nb = normals_blob(m)
		m.bspx = [(b"MVDSV_PHYSICSNORMALS", nb)]
		yield Case("pn_bspx_" + f, "bspx", m.serialize(), "OK~normals=bspx",
		           "BSPX physics normals of the right size: loaded (the oracle compares them)")
		m = base(f)
		m.bspx = [(b"RGBLIGHTING", b"\1\2\3" * 3), (b"MVDSV_PHYSICSNORMALS", nb[:-16])]
		yield Case("pn_bspx_short_" + f, "bspx", m.serialize(), NO_NORMALS, "normals lump 16 bytes short: ignored")
		m = base(f)
		m.bspx = [(b"MVDSV_PHYSICSNORMALS", nb + bytes(16))]
		yield Case("pn_bspx_long_" + f, "bspx", m.serialize(), NO_NORMALS, "normals lump 16 bytes long: ignored")
		m = base(f)
		m.bspx = [(b"MVDSV_PHYSICSNORMALS", nb[:8])]
		yield Case("pn_bspx_header_" + f, "bspx", m.serialize(), NO_NORMALS, "normals lump with only the 8-byte header")

	f = "v29"
	m = base(f)
	nb = normals_blob(m)
	m.bspx = []
	yield Case("bspx_empty", "bspx", m.serialize(), "OK", "BSPX directory with 0 lumps")
	m = base(f)
	m.bspx = [(b"RGBLIGHTING", b"\1\2\3" * 4)]
	yield Case("bspx_other", "bspx", m.serialize(), "OK", "BSPX with a lump cmodel.c does not read")
	m = base(f)
	m.bspx = [(b"MVDSV_PHYSICSNORMALS_AND_SOME_MORE_X", nb)]
	yield Case("bspx_name24", "bspx", m.serialize(), NO_NORMALS,
	           "a 24-byte lump name with no NUL: the loader terminates it (MVDSV_PHYSICSNORM...), so not found")

	# numlumps: CM_LoadBSPX computes lumpssize = 32 * numlumps in an int
	for n, exp, why in (
		(-1, "OK", "negative count: 'Corrupt BSPX header', ignored"),
		(0x04000000, "OK", "32 * n = 2^31 is negative as int: rejected, ignored"),
		(0x08000000, "OK", "32 * n = 2^32 is 0 as int: the size check passes, CM_BSPX_LoadLumps walks 2^27 "
		                   "entries of a 0-byte buffer (heap overflow, read and write). Fix: ignore the directory"),
		(0x08000001, "OK", "32 * n wraps to 32: as above with a 32-byte buffer"),
		(0x7fffffff, "OK|SYS_ERROR", "32 * n is -32 as int: the size check passes, Hunk_TempAllocMore(-32) "
		                            "Sys_Errors (policy: a clean Sys_Error may stay; ignoring is better)"),
	):
		yield Case("bspx_numlumps_%x" % (n & 0xffffffff), "bspx",
		           with_trailer(base(f), bspx_dir([(b"MVDSV_PHYSICSNORMALS", nb)], numlumps=n)), exp, why)

	m = base(f)
	d = with_trailer(m, bspx_dir([(b"MVDSV_PHYSICSNORMALS", nb)]))
	x = len(m.serialize()) + (-len(m.serialize()) % 4)
	yield Case("bspx_entry_outside", "bspx", put(d, "<i", x + 8 + 28, 1 << 20), "OK",
	           "entry runs past the end of the file: 'Invalid BSPX lump position', ignored")
	yield Case("bspx_entry_ofs_overflow", "bspx", put(d, "<ii", x + 8 + 24, 0x7ffffff0, 0x20), "OK",
	           "fileofs + filelen overflows int (UB, then rejected): ignored")
	yield Case("bspx_entry_neg", "bspx", put(d, "<i", x + 8 + 28, -8), "OK", "negative entry length: ignored")
	yield Case("bspx_no_magic", "bspx", put(d, "4s", x, b"XXXX"), "OK",
	           "no 'BSPX' magic: cmodel.c does not check it (normals still load); either way OK")

	# the loader looks at the exact end of the last lump, BSPX writers pad to 4
	m = base(f)
	m.entities += b"\0" * ((-len(m.entities)) % 4 + 1)  # odd length
	m.place = [(99 if i == LUMP_ENTITIES else i, 0) for i in range(15)]  # entities last
	m.bspx = [(b"MVDSV_PHYSICSNORMALS", nb)]
	yield Case("bspx_after_padding", "bspx", m.serialize(), "OK",
	           "last lump ends unaligned, BSPX after the padding: cmodel.c reads the directory 3 bytes early "
	           "(normals not found; 0 of 165 real BSPX maps are affected)")

	# external .qpn next to the map (names <= 31 chars: COM_FileBase)
	for f in ("v29", "bsp2"):
		m = base(f)
		yield Case("qpn_%s" % f, "bspx", m.serialize(), "OK~normals=qpn", "external .qpn of the right size: loaded",
		           {".qpn": normals_blob(m, flip=True)})
		yield Case("qpn_bad_%s" % f, "bspx", m.serialize(), NO_NORMALS, "external .qpn of the wrong size: ignored",
		           {".qpn": normals_blob(m, flip=True)[:-4]})
		yield Case("qpn_empty_%s" % f, "bspx", m.serialize(), NO_NORMALS, "empty external .qpn: ignored",
		           {".qpn": b""})
		m2 = base(f)
		m2.bspx = [(b"MVDSV_PHYSICSNORMALS", normals_blob(m2))]
		yield Case("qpn_over_bspx_%s" % f, "bspx", m2.serialize(), "OK~normals=qpn",
		           "both a BSPX lump and a .qpn: the .qpn wins", {".qpn": normals_blob(m2, flip=True)})


def node_boundary(f, u):
	"""a map whose world tree has a child u = numnodes - 1 (the cell split
	node), with u - 12 unreachable padding nodes before it"""
	m = base(f, cells=2, subs=1)
	permute_nodes(m, list(range(6)) + list(range(7, 13)) + [6])
	pad = u - 12
	m.pad_nodes(12, pad)
	assert m.nodes[5][1] == u and len(m.nodes) == u + 1
	return m, pad


def clip_boundary(f, u):
	"""the world hull 1 chain is last, ending at clipnode u = numclipnodes - 1"""
	m = base(f, subs=1)
	n = len(m.clipnodes)
	# world hull 1 is clipnodes 0..5: move it to the end
	permute_clipnodes(m, list(range(6, n)) + list(range(6)))
	pad = u + 1 - n
	m.pad_clipnodes(n - 6, pad)
	assert len(m.clipnodes) == u + 1 and m.clipnodes[u][2] in (CONTENTS_EMPTY, CONTENTS_SOLID)
	return m, pad


def fam_unsigned():
	for f in F16:
		for u in (32766, 32767, 32768, 65534, 65535):
			m, pad = node_boundary(f, u)
			yield Case("nodes_child%d_%s" % (u, f), "unsigned", m.serialize(),
			           r"OK~unreached_nodes=%d\b" % pad,
			           "%d nodes, the world tree's child %d is the last one. Read as unsigned 16-bit (QuakeSpasm's "
			           "rule, as oms2 needs) it is that node; read as signed it is leaf %d" % (u + 1, u, 65535 - u)
			           + (" (unreached_nodes shows the misread)" if u >= 32768 else ""))
		for u in (32767, 32768, 32772, 65535):
			m, pad = clip_boundary(f, u)
			yield Case("clip_child%d_%s" % (u, f), "unsigned", m.serialize(),
			           r"OK~unreached_clipnodes=%d\b" % pad,
			           "%d clipnodes, world hull 1 is the last 6 (children up to %d): read as unsigned, "
			           "child >= numclipnodes is contents (child - 65536)" % (u + 1, u))


def deep_chain(f, depth, hull=0):
	"""depth redundant splits (plane x = 4096, the room is always behind it)
	in front of the world's hull `hull` tree"""
	m = base(f)
	p = m.plane(0, 4096)
	if hull == 0:
		first = len(m.nodes)
		inner = m.nodes[5][1]
		for i in range(depth):
			m.nodes.append([p, leaf(0), first + i + 1 if i < depth - 1 else inner, [0] * 3, [0] * 3])
		m.nodes[5][1] = first
	else:
		clip_chain(m, hull, depth, lambda i, nxt: [p, CONTENTS_SOLID, nxt])
	return m


def clip_chain(m, hull, depth, record):
	"""Put `depth` clipnodes in front of the world's hull `hull`. They go
	before its clipnodes: the engine reads a hull's headnode as the lowest
	index it may visit (CM_HullPointContents: num < firstclipnode is an error)."""
	head = m.models[0][3][hull]
	m.pad_clipnodes(head, depth)
	for i in range(depth):
		m.clipnodes[head + i] = record(i, head + i + 1)
	m.models[0][3][hull] = head


def fam_trees():
	for f in FORMATS:
		m = base(f)
		m.nodes[3][1] = 3
		yield Case("node_self_" + f, "trees", m.serialize(), REJECT,
		           "a node is its own child: CM_SetParent recurses until the stack overflows")
		m = base(f)
		m.nodes[5][1] = 0
		yield Case("node_cycle_" + f, "trees", m.serialize(), REJECT, "the tree loops back to node 0")
		# a DAG: 40 levels, both children of each node are the next node
		m = base(f)
		first = len(m.nodes)
		inner = m.nodes[5][1]
		p = m.plane(0, 0)
		for i in range(40):
			nxt = first + i + 1 if i < 39 else inner
			m.nodes.append([p, nxt, nxt, [0] * 3, [0] * 3])
		m.nodes[5][1] = first
		yield Case("node_dag_" + f, "trees", m.serialize(), REJECT,
		           "40 levels whose both children are the next node: CM_SetParent and CM_FindTouchedLeafs take "
		           "2^40 steps. Real maps never share nodes, so a loader can reject it")
		for d in (659, 5000):
			yield Case("node_chain%d_%s" % (d, f), "trees", deep_chain(f, d).serialize(),
			           "OK" if d == 659 else "OK|" + REJECT,
			           "a chain %d nodes deep (real maximum: 659; a depth limit must allow that)" % d)
		m = base(f)
		h1 = m.models[0][3][1]
		m.clipnodes[h1 + 2][2] = h1
		yield Case("clip_cycle_" + f, "trees", m.serialize(), REJECT,
		           "a clipnode cycle in world hull 1: point contents loops forever, a trace recurses until the "
		           "stack overflows")
		yield Case("clip_chain659_" + f, "trees", deep_chain(f, 659, hull=1).serialize(), "OK",
		           "world hull 1 is a chain 659 clipnodes deep")
		yield Case("clip_chain5000_" + f, "trees", deep_chain(f, 5000, hull=1).serialize(), "OK|" + REJECT,
		           "world hull 1 is a chain 5000 clipnodes deep: traces recurse that deep")
	yield Case("node_chain30000_v29", "trees", deep_chain("v29", 30000).serialize(), "OK|" + REJECT,
	           "a chain 30000 nodes deep: CM_SetParent recursion (stack overflow on small stacks)")
	yield Case("node_chain100000_bsp2", "trees", deep_chain("bsp2", 100000).serialize(), "OK|" + REJECT,
	           "a chain 100000 nodes deep")
	m = base("hl")
	p = m.plane(0, 0)
	clip_chain(m, 1, 40, lambda i, nxt: [p, nxt, nxt])
	yield Case("clip_dag_hl", "trees", m.serialize(), "OK",
	           "40 levels of shared clipnodes in world hull 1. Unlike a node DAG this is harmless: a trace splits "
	           "its segment at each plane, so the work stays linear. Real maps share clipnodes (HL lamothe.bsp: 61)")


def fam_vis():
	for f in FORMATS:
		m = base(f)
		nl = len(m.leafs)
		for v, exp, why in (
			(nl, REJECT, "visleafs == numleafs: the PVS build reads one leaf record past the lump"),
			(nl - 1, "OK", "visleafs == numleafs - 1: the largest valid value (the extra rows are submodel leafs)"),
			(0, REJECT, "visleafs 0, the world tree reaches leafs 1..8: CM_LeafPVS reads before the PVS"),
			(4, REJECT, "visleafs below the world's leafs: CM_LeafPVS reads past the PVS"),
			(-1, REJECT, "negative visleafs"),
			(-100, REJECT, "negative visleafs: the row size goes negative"),
			(0x7fffffff, REJECT, "visleafs INT_MAX: (visleafs + 31) overflows"),
		):
			m = base(f)
			m.models[0][4] = v
			if 0 < v < len(m.leafs):
				m.set_vis(1)
			yield Case("visleafs_%s_%s" % ({nl: "eq_numleafs", nl - 1: "max"}.get(v, str(v)), f), "vis",
			           m.serialize(), exp, why)

		vl = base(f).models[0][4]
		m = base(f)
		m.leafs[2][1] = len(m.vis) + 10
		yield Case("visofs_past_" + f, "vis", m.serialize(), REJECT, "visofs past the end of the vis lump")
		m = base(f)
		m.leafs[2][1] = -2
		yield Case("visofs_neg_" + f, "vis", m.serialize(), REJECT, "visofs -2 reads before the vis lump")
		m = base(f)
		m.vis += b"\0"
		m.leafs[vl][1] = len(m.vis) - 1
		yield Case("vis_run_at_end_" + f, "vis", m.serialize(), "OK|" + REJECT,
		           "the last row is a zero run whose count byte is past the end of the lump")
		m = base(f, cells=12)
		m.vis += b"\xff"
		m.leafs[5][1] = len(m.vis) - 1
		yield Case("vis_row_past_end_" + f, "vis", m.serialize(), "OK|" + REJECT,
		           "a 2-byte row whose second byte is past the end of the lump")
		m = base(f)
		m.vis += b"\0\xff"
		m.leafs[2][1] = len(m.vis) - 2
		yield Case("vis_run_long_" + f, "vis", m.serialize(), "OK",
		           "a 255-byte zero run in a 1-byte row: DecompressVis writes 255 bytes into its 4095-byte buffer, "
		           "the row copied out is 0")

	# boundaries of the fixed-size buffers: fatpvs[4095] (server) and DecompressVis's 4095 bytes
	for f, v, why in (
		("v29", 32736, "the largest visleafs whose fat PVS fits fatpvs[4095]"),
		("v29", 32737, "PVS rows are 4096 bytes: CM_BuildPVS copies a row out of DecompressVis's 4095-byte "
		               "buffer (read overflow), CM_FatPVS writes 4096 bytes into fatpvs[4095] (server)"),
		("bsp2", 32737, "as above, BSP2"),
		("bsp2", 32760, "the largest visleafs whose decompressed row fits DecompressVis's buffer (the copy and "
		                "the fat PVS still overflow)"),
		("bsp2", 32761, "a decompressed row of 4096 bytes: DecompressVis also writes 1 byte past its buffer"),
	):
		yield Case("visleafs%d_%s" % (v, f), "vis", base(f, cells=v, subs=0, vis="all").serialize(), "OK", why)
	yield Case("visleafs32737_novis_v29", "vis", base("v29", cells=32737, subs=0, vis="none").serialize(), "OK",
	           "no vis lump, 4096-byte rows: only CM_FatPVS overflows (server; the 40 real BSP2 maps)")
	m = base("bsp2", cells=32000, subs=0, vis="all")
	row = bytes([0xff]) * 3999 + b"\0\xff"
	m.leafs[1][1] = len(m.vis)
	m.vis += row
	yield Case("vis_run_overflow_bsp2", "vis", m.serialize(), "OK|" + REJECT,
	           "a 4000-byte row ending in a 255-byte zero run: DecompressVis writes 159 bytes past its buffer")
	m = base("v29", vis="none")
	m.models[0][4] = 131072
	m.leafs += [[CONTENTS_EMPTY, -1, [0] * 3, [0] * 3, [0] * 4] for _ in range(131073 - len(m.leafs) + 1)]
	yield Case("visleafs131072_v29", "vis", m.serialize(), REJECT,
	           "rowbytes * visleafs = 2^31 overflows int: master Sys_Errors on a negative hunk size "
	           "(4 real maps); policy: check the multiplication, Host_Error")


def fam_layout():
	f = "v29"
	b = base(f).serialize()
	for n in (0, 3, 4, HEADER_SIZE - 1, HEADER_SIZE, len(b) // 2, len(b) - 1):
		yield Case("truncated_%d" % n, "layout", b[:n], "NULL", "the file cut at %d of %d bytes" % (n, len(b)))
	yield Case("bad_version", "layout", put(b, "<i", 0, 28), REJECT, "version 28")
	yield Case("overlap_textures", "layout",
	           set_lump(b, LUMP_TEXTURES, *struct.unpack_from("<ii", b, lump_entry(LUMP_ENTITIES))), "OK",
	           "the texture lump overlaps the entity lump (cmodel.c does not read textures)")
	yield Case("trailing_zeros", "layout", b + bytes(64), "OK",
	           "64 zero bytes after the last lump: read as a BSPX directory with 0 lumps")

	for f in FORMATS:
		b = base(f).serialize()
		for i in (LUMP_NODES, LUMP_LEAFS, LUMP_CLIPNODES) if f != "v29" else \
				(LUMP_PLANES, LUMP_NODES, LUMP_LEAFS, LUMP_CLIPNODES, LUMP_MODELS):
			o, l = struct.unpack_from("<ii", b, lump_entry(i))
			yield Case("funny_%s_%s" % (LUMP_NAMES[i], f), "layout", set_lump(b, i, length=l - 1), REJECT,
			           "%s lump length not a multiple of the record size: 'funny lump size'" % LUMP_NAMES[i])
		for i, exp, why in (
			(LUMP_NODES, REJECT, "no nodes: CM_SetParent reads node 0 of an empty array"),
			(LUMP_LEAFS, REJECT, "no leafs: node children point into an empty array"),
			(LUMP_PLANES, REJECT, "no planes: every node's plane is out of range"),
		):
			yield Case("zero_%s_%s" % (LUMP_NAMES[i], f), "layout", set_lump(b, i, length=0), exp, why)
		m = base(f)
		m.clipnodes = []
		for mdl in m.models:
			mdl[3][1:] = [0, 0, 0]
		yield Case("zero_clipnodes_" + f, "layout", m.serialize(),
		           "OK" if f == "hl" else "INVARIANT~world hull 1 is empty",
		           "no clipnodes, every hull 1-3 headnode 0 (Bonk Jam tele*): " +
		           ("Half-Life reads the hulls as empty" if f == "hl" else
		            "loads, the first player move Sys_Errors. Policy: kept (the PR's count <= 0 rejects it)"))
	b = base("v29").serialize()
	yield Case("zero_entities", "layout", set_lump(b, LUMP_ENTITIES, length=0), "OK",
	           "no entity lump: CM_EntityString() is NULL")
	yield Case("zero_models", "layout", set_lump(b, LUMP_MODELS, length=0), REJECT, "'Map with no models'")
	yield Case("zero_vis", "layout", set_lump(b, LUMP_VISIBILITY, length=0), "OK", "no vis lump: all visible")
	end = len(b)
	for i, o, l, exp, why in (
		(LUMP_PLANES, end + 100, None, "NULL", "planes start past the end of the file"),
		(LUMP_ENTITIES, end + 100, None, "NULL", "entities start past the end (the checksum skips entities)"),
		(LUMP_PLANES, -4, None, "NULL", "negative planes offset"),
		(LUMP_PLANES, None, end, "NULL", "planes run past the end of the file"),
		(LUMP_PLANES, 0x7ffffff0, 0x20, "NULL", "planes offset + length overflows int"),
		(LUMP_ENTITIES, 0x7ffffff0, 0x20, "NULL",
		 "entities offset + length overflows int: CM_BSPX_FindOffset adds them (signed overflow, UB)"),
		(LUMP_PLANES, None, -20, "SYS_ERROR|HOST_ERROR|NULL",
		 "negative planes length: CM_CalcChecksum's Hunk_TempAlloc(-20) Sys_Errors. Policy: a clean Sys_Error may stay"),
		(LUMP_ENTITIES, None, -20, "SYS_ERROR|HOST_ERROR|NULL", "negative entities length: as above, in CM_ReadLump"),
		(LUMP_PLANES, None, 0x7fffff00, "NULL|OOM|SYS_ERROR|HOST_ERROR",
		 "a 2 GB planes length in a 2 KB file: CM_CalcChecksum allocates it before reading (the engine's hunk "
		 "Sys_Errors; the harness without a cap returns NULL)"),
	):
		yield Case("lump_%s_%s" % (LUMP_NAMES[i], ("ofs%d" % o if o is not None else "len%d" % l).replace("-", "m")),
		           "layout", set_lump(b, i, o, l), exp, why)


def fam_ranges():
	for f in FORMATS:
		hl = f == "hl"
		m0 = base(f)
		ncl, npl, nn, nl = len(m0.clipnodes), len(m0.planes), len(m0.nodes), len(m0.leafs)
		h1 = m0.models[0][3][1]

		m = base(f)
		m.clipnodes[h1][2] = ncl + 5
		yield Case("clip_child_oor_" + f, "ranges", m.serialize(), r"INVARIANT~child %d outside" % (ncl + 5),
		           "a clipnode child past the clipnodes: the engine Sys_Errors when a move reaches it. Policy: kept")
		for v, tag in ((npl + 1000, "oor"), (-1, "neg")):
			m = base(f)
			m.clipnodes[3][0] = v
			yield Case("clip_plane_%s_%s" % (tag, f), "ranges", m.serialize(), REJECT,
			           "clipnode planenum %d: CM_LoadPhysicsNormals reads map_planes[planenum] at load" % v)
			m = base(f)
			m.nodes[1][0] = v
			yield Case("node_plane_%s_%s" % (tag, f), "ranges", m.serialize(), REJECT,
			           "node planenum %d (#615 PoCs 75, 87): the first point lookup reads past the planes" % v)
		m = base(f)
		m.models[0][3][1] = ncl
		yield Case("world_hull1_empty_" + f, "ranges", m.serialize(),
		           "OK" if hl else "INVARIANT~world hull 1 is empty",
		           "world hull 1 headnode = numclipnodes, qbsp's empty hull. " +
		           ("Half-Life reads it as empty" if hl else "The first move Sys_Errors. Policy: kept"))
		m = base(f)
		m.models[1][3][2] = ncl
		yield Case("sub_hull2_empty_" + f, "ranges", m.serialize(), "OK" if hl else "OK~empty_hulls=1",
		           "submodel hull 2 headnode = numclipnodes (kdm2, rubicon: visual-only models): loads")
		m = base(f)
		m.models[1][3][2] = ncl + 7
		yield Case("sub_hull2_oor_" + f, "ranges", m.serialize(), r"INVARIANT~headnode %d outside" % (ncl + 7),
		           "submodel headnode past the clipnodes: Sys_Error when traced. Policy: kept")
		m = base(f)
		m.models[1][3][0] = nn + 5
		yield Case("sub_hull0_oor_" + f, "ranges", m.serialize(), r"INVARIANT~headnode %d outside" % (nn + 5),
		           "submodel hull 0 headnode past the nodes: Sys_Error when traced. Policy: kept")
		for v in (-1, -2):
			m = base(f)
			m.models[1][3][1] = v
			yield Case("sub_hull1_contents%d_%s" % (-v, f), "ranges", m.serialize(), "OK",
			           "a submodel hull that is all one contents (headnode %d)" % v)
		m = base(f)
		m.models[0][3][0] = 6
		yield Case("world_headnode6_" + f, "ranges", m.serialize(), "OK~world_headnode=6",
		           "world hull 0 headnode 6: the engine always starts at node 0 (a note)")
		m = base(f)
		m.nodes[2][2] = nn + 3
		yield Case("node_child_oor_" + f, "ranges", m.serialize(), REJECT,
		           "a node child past the nodes: CM_SetParent writes outside the array")
		m = base(f)
		m.nodes[2][2] = leaf(nl + 3)
		yield Case("node_leaf_oor_" + f, "ranges", m.serialize(), REJECT,
		           "a node child past the leafs: CM_SetParent writes outside the array")
		for c in (0, 5):
			m = base(f)
			m.leafs[2][0] = c
			yield Case("leaf_contents%d_%s" % (c, f), "ranges", m.serialize(), REJECT,
			           "a leaf with contents %d >= 0 under a node: read as a node, past the leaf's end" % c)
		m = base(f)
		k = m.nodes[0][0]
		m.planes[k][1], m.planes[k][4] = 2e38, 3
		yield Case("plane_huge_normal_" + f, "ranges", m.serialize(), r"OK~bad_planes=1\b",
		           "node 0's plane with normal y 2e38, type 3 (phase 6 triage): the dot product overflows and "
		           "a trace across it gets fraction NaN, like a NaN plane (a note; no real map has one). Policy: kept")
		m = base(f)
		m.leafs[0][0] = -1
		yield Case("leaf0_empty_" + f, "ranges", m.serialize(), REJECT,
		           "leaf 0 (the shared solid leaf) with contents -1 (phase 6 triage): CM_FindTouchedLeafs "
		           "returns it, and the server reads pvs[-1] (leafnum - 1)")

	for n, exp, why in ((300, "OK", "300 models (the probes walk the first 256)"),
	                    (4096, "OK", "MAX_MAP_MODELS models"),
	                    (4097, REJECT, "'Map has too many models'")):
		m = base("v29")
		m.models += [list(map(list, m.models[1][:3])) + [list(m.models[1][3]), 0] for _ in range(n - len(m.models))]
		yield Case("models%d" % n, "ranges", m.serialize(), exp, why)


FAMILIES = (fam_base, fam_bspx, fam_unsigned, fam_trees, fam_vis, fam_layout, fam_ranges)


def all_cases():
	seen = set()
	for fam in FAMILIES:
		for c in fam():
			assert c.name not in seen, c.name
			assert len(c.name) <= 31 or not c.extra, c.name  # COM_FileBase keeps 31 chars
			seen.add(c.name)
			yield c


# ------------------------------------------------------------------ cases

def write_cases(out):
	os.makedirs(out, exist_ok=True)
	for f in os.listdir(out):
		if f.endswith((".bsp", ".qpn")):
			os.unlink(os.path.join(out, f))
	rows = []
	total = 0
	for c in all_cases():
		open(os.path.join(out, c.name + ".bsp"), "wb").write(c.data)
		for sfx, data in c.extra.items():
			open(os.path.join(out, c.name + sfx), "wb").write(data)
		total += len(c.data)
		rows.append((c.name, c.family, c.expect, len(c.data), hashlib.sha256(c.data).hexdigest(), c.why))
	# big cases are rebuilt by this command (expected.tsv has their SHA-256), not committed
	with open(os.path.join(out, ".gitignore"), "w", newline="\n") as f:
		f.write("# written by tools/bspmut.py cases: cases over 1 MB, rebuilt by it\n")
		f.writelines("/%s.bsp\n" % r[0] for r in rows if r[3] > 1 << 20)
	with open(os.path.join(out, "expected.tsv"), "w", newline="", encoding="utf-8") as f:
		w = csv.writer(f, delimiter="\t", lineterminator="\n")
		w.writerow(("case", "family", "expect", "bytes", "sha256", "why"))
		w.writerows(rows)
	fams = {}
	for r in rows:
		fams[r[1]] = fams.get(r[1], 0) + 1
	print("%d cases, %.1f MB, in %s (%s)" % (len(rows), total / 1e6, out,
	                                         ", ".join("%s %d" % kv for kv in fams.items())))


def write_seeds(out):
	"""Seeds for phase 6: the formats and the BSPX code that the 32 KB seeds
	from real maps lack (no 2PSB, HL or BSPX seed). All load OK on master."""
	os.makedirs(out, exist_ok=True)
	n = 0
	for c in fam_base():
		open(os.path.join(out, c.name + ".bsp"), "wb").write(c.data)
		n += 1
	for c in fam_bspx():
		if any(c.name == "pn_bspx_" + f for f in FORMATS):
			open(os.path.join(out, c.name + ".bsp"), "wb").write(c.data)
			n += 1
	print("%d seeds in %s" % (n, out))


def read_expected(d):
	with open(os.path.join(d, "expected.tsv"), encoding="utf-8") as f:
		return list(csv.DictReader(f, delimiter="\t"))


def matches(expect, status, msg):
	outs, _, rx = expect.partition("~")
	return status in outs.split("|") and (not rx or re.search(rx, msg) is not None)


def check(a):
	exp = read_expected(a.cases)
	paths = [os.path.abspath(os.path.join(a.cases, e["case"] + ".bsp")) for e in exp]
	for e, p in zip(exp, paths):
		if hashlib.sha256(open(p, "rb").read()).hexdigest() != e["sha256"]:
			sys.exit("%s differs from expected.tsv: rerun 'bspmut.py cases'" % p)
	sys.path.insert(0, os.path.dirname(HERE))
	import run
	res = run.run_all(os.path.abspath(a.exe), paths, a.jobs, a.client, a.timeout)
	fails = 0
	rows = []
	for e, p in zip(exp, paths):
		st, msg = res.get(p, ("MISSING", ""))
		ok = matches(e["expect"], st, msg)
		fails += not ok
		rows.append((e["case"], e["family"], e["expect"], st, msg, "pass" if ok else "FAIL"))
		if a.verbose or not ok:
			print("%-4s %-30s expect %-28s got %s %s" % ("ok" if ok else "FAIL", e["case"], e["expect"][:28], st, msg[:90]))
	if a.out:
		with open(a.out, "w", newline="", encoding="utf-8") as f:
			w = csv.writer(f, delimiter="\t", lineterminator="\n")
			w.writerow(("case", "family", "expect", "status", "message", "verdict"))
			w.writerows(rows)
	byfam = {}
	for r in rows:
		t = byfam.setdefault(r[1], [0, 0])
		t[0] += r[5] == "pass"
		t[1] += 1
	print("%s%s: %d/%d as expected (%s)" % (os.path.basename(a.exe), " client" if a.client else "",
	                                       len(rows) - fails, len(rows),
	                                       ", ".join("%s %d/%d" % (k, v[0], v[1]) for k, v in byfam.items())))
	return 1 if fails else 0


def table(paths):
	"""per family: cases as expected, per results file; then the cases where they differ"""
	res = []
	for p in paths:
		with open(p, encoding="utf-8") as f:
			res.append({r["case"]: r for r in csv.DictReader(f, delimiter="\t")})
	names = [os.path.splitext(os.path.basename(p))[0] for p in paths]
	cases = list(res[0])
	fams = []
	for c in cases:
		if res[0][c]["family"] not in fams:
			fams.append(res[0][c]["family"])
	print("| family | cases | " + " | ".join(names) + " |")
	print("|---|---|" + "---|" * len(names))
	for fam in fams + ["all"]:
		cs = [c for c in cases if fam == "all" or res[0][c]["family"] == fam]
		print("| %s | %d | " % (fam, len(cs)) + " | ".join(
			str(sum(r[c]["verdict"] == "pass" for c in cs if c in r)) for r in res) + " |")


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	sub = ap.add_subparsers(dest="cmd", required=True)
	c = sub.add_parser("cases")
	c.add_argument("out", nargs="?", default=os.path.join(os.path.dirname(HERE), "cases", "synthetic"))
	k = sub.add_parser("check")
	k.add_argument("exe")
	k.add_argument("--cases", default=os.path.join(os.path.dirname(HERE), "cases", "synthetic"))
	k.add_argument("-c", "--client", action="store_true")
	k.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
	k.add_argument("-t", "--timeout", type=int, default=60)
	k.add_argument("-o", "--out")
	k.add_argument("-v", "--verbose", action="store_true")
	t = sub.add_parser("table")
	t.add_argument("results", nargs="+")
	e = sub.add_parser("seeds")
	e.add_argument("out", nargs="?", default=os.path.join(os.path.dirname(HERE), "fuzz", "seeds_synth"))
	a = ap.parse_args()
	if a.cmd == "cases":
		write_cases(a.out)
	elif a.cmd == "check":
		sys.exit(check(a))
	elif a.cmd == "seeds":
		write_seeds(a.out)
	else:
		table(a.results)


if __name__ == "__main__":
	main()
