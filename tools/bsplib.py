#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
One BSP read/write library for the bspfuzz tools.

Formats: Quake BSP29 ("v29"), Half-Life BSP30 ("hl"), BSP2 ("bsp2") and
BSP29a / 2PSB ("2psb"), plus the BSPX extension lump directory that follows
the last lump. Everything here is plain struct arithmetic over bytes; nothing
is trusted, so it can describe broken and fuzzed files too.

  from bsplib import Bsp, parse_header, build
  b = Bsp(open(path, "rb").read())
  b.fmt, b.lumps, b.count(LUMP_NODES), b.visleafs, b.bspx, b.stats()

Tools import it with sys.path set to their own directory (they run under
`python -I`, which leaves the script's directory off sys.path).
"""
import struct

HEADER_SIZE = 4 + 15 * 8  # version/magic + 15 (fileofs, filelen) pairs

(LUMP_ENTITIES, LUMP_PLANES, LUMP_TEXTURES, LUMP_VERTEXES, LUMP_VISIBILITY, LUMP_NODES, LUMP_TEXINFO,
 LUMP_FACES, LUMP_LIGHTING, LUMP_CLIPNODES, LUMP_LEAFS, LUMP_MARKSURFACES, LUMP_EDGES, LUMP_SURFEDGES,
 LUMP_MODELS) = range(15)
LUMP_NAMES = ("entities", "planes", "textures", "vertexes", "visibility", "nodes", "texinfo", "faces",
              "lighting", "clipnodes", "leafs", "marksurfaces", "edges", "surfedges", "models")

# the lumps cmodel.c reads (CM_LoadMap); CM_CalcChecksum reads all of them, but
# only to checksum them. Entities are only copied (map_entitystring), not parsed.
CMODEL_LUMPS = frozenset((LUMP_ENTITIES, LUMP_PLANES, LUMP_VISIBILITY, LUMP_NODES, LUMP_CLIPNODES,
                          LUMP_LEAFS, LUMP_MODELS))
# BSPX lumps cmodel.c reads (CM_LoadMap -> CM_BSPX_ReadLump)
CMODEL_BSPX = frozenset((b"MVDSV_PHYSICSNORMALS",))

# version int / magic -> format name
FORMATS = {29: "v29", 30: "hl", b"BSP2": "bsp2", b"2PSB": "2psb"}
VERSION = {"v29": struct.pack("<i", 29), "hl": struct.pack("<i", 30), "bsp2": b"BSP2", "2psb": b"2PSB"}

# struct sizes (bspfile.h): dleaf, dnode, dclipnode, dface, dedge, marksurface
_SIZES = {
	"v29":  dict(leafs=28, nodes=24, clipnodes=8,  faces=20, edges=4, marksurfaces=2),
	"hl":   dict(leafs=28, nodes=24, clipnodes=8,  faces=20, edges=4, marksurfaces=2),
	"bsp2": dict(leafs=44, nodes=44, clipnodes=12, faces=28, edges=8, marksurfaces=4),
	"2psb": dict(leafs=32, nodes=32, clipnodes=12, faces=20, edges=8, marksurfaces=4),
}
for _s in _SIZES.values():
	_s.update(planes=20, models=64, texinfo=40, vertexes=12, surfedges=4)


def struct_size(fmt, lump):
	"""sizeof one record of `lump` (index) in `fmt`, or None (entities, textures, vis, lighting)."""
	return _SIZES.get(fmt, {}).get(LUMP_NAMES[lump])


DMODEL_HEADNODE, DMODEL_VISLEAFS = 36, 52  # dmodel_t: mins, maxs, origin, headnode[4], visleafs, ...
BSPX_ENTRY = 32                            # bspx_lump_t: char name[24], int fileofs, int filelen


def detect(b):
	"""Format name from the first 4 bytes, or None."""
	if len(b) < 4:
		return None
	return FORMATS.get(bytes(b[:4])) or FORMATS.get(struct.unpack_from("<i", b)[0])


def parse_header(b):
	"""(fmt, [(fileofs, filelen)] * 15) from at least HEADER_SIZE bytes; fmt None if not a BSP.
	Works on just the header (qd_sniff reads only the first few KB of a map)."""
	fmt = detect(b)
	if fmt is None or len(b) < HEADER_SIZE:
		return fmt, None
	return fmt, [struct.unpack_from("<ii", b, 4 + 8 * i) for i in range(15)]


def header_counts(fmt, lumps):
	"""Record counts that follow from the lump table alone (no lump data needed)."""
	c = {}
	for name in ("planes", "nodes", "clipnodes", "leafs", "models"):
		i = LUMP_NAMES.index(name)
		c[name] = lumps[i][1] // struct_size(fmt, i)
	c["vis"] = lumps[LUMP_VISIBILITY][1]
	return c


class Bsp:
	def __init__(self, data):
		self.data = bytes(data)
		self.fmt, self.lumps = parse_header(self.data)

	@property
	def ok(self):
		"""A known format with a full header."""
		return self.lumps is not None

	def lump_in_file(self, i):
		o, l = self.lumps[i]
		return o >= 0 and l >= 0 and o + l <= len(self.data)

	@property
	def lumps_in_file(self):
		"""Every lump is inside the file: what CM_CalcChecksum (master) needs to read them all."""
		return self.ok and all(self.lump_in_file(i) for i in range(15))

	def lump(self, i):
		o, l = self.lumps[i]
		return self.data[max(o, 0):max(o, 0) + max(l, 0)]

	def count(self, i):
		return self.lumps[i][1] // struct_size(self.fmt, i)

	def model(self, m):
		"""(headnode[4], visleafs) of submodel m, or None if not in the file."""
		o, l = self.lumps[LUMP_MODELS]
		at = o + m * 64
		if m < 0 or (m + 1) * 64 > l or at < 0 or at + 64 > len(self.data):
			return None
		return (struct.unpack_from("<4i", self.data, at + DMODEL_HEADNODE),
		        struct.unpack_from("<i", self.data, at + DMODEL_VISLEAFS)[0])

	@property
	def visleafs(self):
		m = self.model(0)
		return m[1] if m else None

	@property
	def bspx_offset(self):
		"""Where CM_BSPX_FindOffset looks: the end of the furthest lump, 4-byte aligned."""
		return (max(o + l for o, l in self.lumps) + 3) & ~3

	@property
	def bspx(self):
		"""The BSPX directory, [(name bytes, fileofs, filelen)], or None if there is none.
		Entries are read as far as the file allows; CM_LoadBSPX's validity checks are
		in bspx_valid."""
		x = self.bspx_offset
		if x < 0 or self.data[x:x + 4] != b"BSPX":
			return None
		n = struct.unpack_from("<i", self.data, x + 4)[0] if x + 8 <= len(self.data) else 0
		out = []
		for i in range(max(0, n)):
			at = x + 8 + BSPX_ENTRY * i
			if at + BSPX_ENTRY > len(self.data):
				break
			name = self.data[at:at + 24].split(b"\0", 1)[0]
			o, l = struct.unpack_from("<ii", self.data, at + 24)
			out.append((name, o, l))
		return out

	@property
	def bspx_valid(self):
		"""The directory passes CM_LoadBSPX (count, every entry inside the file)."""
		x, d = self.bspx_offset, self.bspx
		if d is None:
			return False
		n = struct.unpack_from("<i", self.data, x + 4)[0]
		if n < 0 or x + 8 + n * BSPX_ENTRY > len(self.data) or len(d) != n:
			return False
		return all(o >= 0 and l >= 0 and o + l <= len(self.data) for _, o, l in d)

	def stats(self):
		"""The structural numbers cmodel.c cares about (the MANIFEST columns)."""
		s = {"format": self.fmt or "other", "bytes": len(self.data)}
		if not self.ok:
			return s
		s.update(header_counts(self.fmt, self.lumps))
		s["visleafs"] = self.visleafs
		s["entities"] = self.lumps[LUMP_ENTITIES][1]
		s["cm_bytes"] = sum(max(self.lumps[i][1], 0) for i in CMODEL_LUMPS)
		d = self.bspx
		s["bspx"] = ",".join(n.decode("latin1") for n, _, _ in (d or [])[:64])
		flags = []
		if not self.lumps_in_file:
			flags.append("lump_outside_file")
		if any(o % 4 for o, _ in self.lumps):
			flags.append("unaligned")
		if self.fmt in ("v29", "hl") and max(s["nodes"], s["clipnodes"]) > 32767:
			flags.append("v29_unsigned_children")  # 16-bit children only valid read as unsigned
		if s["vis"] <= 0:
			flags.append("novis")
		if (s["visleafs"] or 0) > 32767:
			flags.append("visleafs_gt_32767")
		if s["clipnodes"] == 0:
			flags.append("no_clipnodes")
		if d is not None and not self.bspx_valid:
			flags.append("bspx_invalid")
		s["flags"] = ",".join(flags)
		return s


def build(fmt, lumps, place=None, bspx=None):
	"""Assemble a BSP file.

	lumps: 15 byte strings (index = lump number).
	place: optional 15 (order_key, residue) pairs: lumps are written in order_key
	       order, each at an offset == residue (mod 4), so a rewritten map keeps the
	       original's lump order and alignment. Default: index order, 4-aligned.
	       Empty lumps get the first offset >= HEADER_SIZE with their residue.
	bspx:  optional [(name bytes, data bytes)]: a BSPX directory after the last lump.
	"""
	place = place or [(i, 0) for i in range(15)]
	out = bytearray(HEADER_SIZE)
	out[0:4] = VERSION[fmt]
	table = [None] * 15
	for i in sorted(range(15), key=lambda i: place[i][0]):
		r = place[i][1] % 4
		if not lumps[i]:
			table[i] = (HEADER_SIZE + (r - HEADER_SIZE) % 4, 0)
			continue
		out += bytes((r - len(out)) % 4)
		table[i] = (len(out), len(lumps[i]))
		out += lumps[i]
	for i, (o, l) in enumerate(table):
		struct.pack_into("<ii", out, 4 + 8 * i, o, l)
	if bspx is not None:
		end = (max(o + l for o, l in table) + 3) & ~3
		out += bytes(end - len(out))
		dir_at = len(out)
		out += b"BSPX" + struct.pack("<i", len(bspx)) + bytes(BSPX_ENTRY * len(bspx))
		for k, (name, data) in enumerate(bspx):
			out += bytes(-len(out) % 4)
			struct.pack_into("<24sii", out, dir_at + 8 + BSPX_ENTRY * k, name[:23], len(out), len(data))
			out += data
	return bytes(out)
