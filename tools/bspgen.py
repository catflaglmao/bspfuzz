#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Synthetic BSP maps built from scratch, in every format cmodel.c reads (v29,
HL BSP30, BSP2, 2PSB). They hold only what the collision loader reads, are a
few KB, and are entirely our own work, so unlike seeds stripped from real
maps they can be committed and published.

The map is a box room split into `cells` empty leafs along x, plus `subs`
door-like submodels (small solid boxes). Hull 0 is a node tree, hulls 1 and 2
(and 3 for Half-Life) are clipnode chains built for the player sizes, the PVS
lets each cell see its neighbours, and the entity lump names every submodel.

  from bspgen import Map
  m = Map.room(fmt="v29", cells=8, subs=1)
  data = m.serialize()                      # bytes of a valid map
  m.nodes[5][1] = ...; m.serialize()        # mutate the structured form first

  python tools/bspgen.py <outdir>           # one base map per format

Records are lists so mutations can edit them in place. Children are stored as
the int the file holds (node index >= 0, leaf -1-index < 0); v29/HL write the
low 16 bits, so 32768..65535 can be written as unsigned children.
"""
import os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bsplib import (build, LUMP_ENTITIES, LUMP_PLANES, LUMP_VISIBILITY, LUMP_NODES, LUMP_CLIPNODES,
                    LUMP_LEAFS, LUMP_MODELS)

CONTENTS_EMPTY, CONTENTS_SOLID = -1, -2
FORMATS = ("v29", "hl", "bsp2", "2psb")

# player hull sizes: (mins, maxs) per hull number
HULLS_Q1 = {1: ((-16, -16, -24), (16, 16, 32)), 2: ((-32, -32, -24), (32, 32, 64))}
HULLS_HL = {1: ((-16, -16, -36), (16, 16, 36)), 2: ((-32, -32, -36), (32, 32, 36)),
            3: ((-16, -16, -18), (16, 16, 18))}


def leaf(i):
	"""the child value that points at leaf i"""
	return -1 - i


def compress_row(row):
	"""Quake vis RLE: zero bytes become (0, run length <= 255)"""
	out, i = bytearray(), 0
	while i < len(row):
		if row[i]:
			out.append(row[i])
			i += 1
			continue
		n = 0
		while i < len(row) and not row[i] and n < 255:
			n += 1
			i += 1
		out += bytes((0, n))
	return bytes(out)


class Map:
	def __init__(self, fmt):
		assert fmt in FORMATS
		self.fmt = fmt
		self.planes = []      # [nx, ny, nz, dist, type]
		self.nodes = []       # [planenum, child0, child1, mins(3), maxs(3)]
		self.leafs = []       # [contents, visofs, mins(3), maxs(3), ambient(4)]
		self.clipnodes = []   # [planenum, child0, child1]
		self.models = []      # [mins(3), maxs(3), origin(3), headnode(4), visleafs]
		self.vis = b""
		self.entities = b""
		self.bspx = None      # [(name bytes, data bytes)] or None
		self.raw = {}         # lump index -> bytes: replaces the serialized lump
		self.place = None     # bsplib.build() placement (order, residue mod 4)
		self.trailer = b""    # bytes appended after everything (e.g. a misplaced BSPX)
		self._plane_ix = {}

	@property
	def halflife(self):
		return self.fmt == "hl"

	@property
	def hull_sizes(self):
		return HULLS_HL if self.halflife else HULLS_Q1

	def plane(self, axis, dist):
		"""index of the axial plane normal[axis] = 1, dist; shared between users"""
		key = (axis, float(dist))
		if key not in self._plane_ix:
			n = [0.0, 0.0, 0.0]
			n[axis] = 1.0
			self._plane_ix[key] = len(self.planes)
			self.planes.append(n + [float(dist), axis])
		return self._plane_ix[key]

	def box_nodes(self, lo, hi, inside, outside):
		"""Append 6 nodes that bound the box lo..hi; returns the first index.
		Points in the box end at `inside`, the rest at `outside` (child values)."""
		first = len(self.nodes)
		for k in range(6):
			axis, upper = k >> 1, not (k & 1)
			nxt = first + k + 1 if k < 5 else inside
			p = self.plane(axis, hi[axis] if upper else lo[axis])
			# front is x > dist: outside for the upper face, inside for the lower
			c = [outside, nxt] if upper else [nxt, outside]
			self.nodes.append([p, c[0], c[1], list(lo), list(hi)])
		return first

	def box_clipnodes(self, lo, hi, hull, inside, outside):
		"""Append 6 clipnodes for the box expanded by `hull` (Minkowski sum, as
		qbsp does); returns the first index"""
		hmins, hmaxs = self.hull_sizes[hull]
		lo = [lo[j] - hmaxs[j] for j in range(3)]
		hi = [hi[j] - hmins[j] for j in range(3)]
		return self._clip_box(lo, hi, inside, outside)

	def room_clipnodes(self, lo, hi, hull):
		"""clipnodes for the inside of a room: the player's origin stays in
		lo - hull mins .. hi - hull maxs"""
		hmins, hmaxs = self.hull_sizes[hull]
		lo = [lo[j] - hmins[j] for j in range(3)]
		hi = [hi[j] - hmaxs[j] for j in range(3)]
		return self._clip_box(lo, hi, CONTENTS_EMPTY, CONTENTS_SOLID)

	def _clip_box(self, lo, hi, inside, outside):
		first = len(self.clipnodes)
		for k in range(6):
			axis, upper = k >> 1, not (k & 1)
			nxt = first + k + 1 if k < 5 else inside
			p = self.plane(axis, hi[axis] if upper else lo[axis])
			self.clipnodes.append([p, outside, nxt] if upper else [p, nxt, outside])
		return first

	def cell_tree(self, x0, x1, cells, first_leaf, shape="balanced"):
		"""Split x0..x1 into `cells` slabs, leafs first_leaf.. in x order.
		Returns the child value of the subtree root."""
		if cells == 1:
			return leaf(first_leaf)
		bounds = [x0 + (x1 - x0) * i / cells for i in range(cells + 1)]
		lo, hi = list(self.room_lo), list(self.room_hi)

		def sub(a, b):  # slabs a..b-1
			if b - a == 1:
				return leaf(first_leaf + a)
			m = (a + b) // 2 if shape == "balanced" else b - 1
			ix = len(self.nodes)
			self.nodes.append([self.plane(0, bounds[m]), 0, 0, lo, hi])
			back = sub(a, m)
			front = sub(m, b)
			self.nodes[ix][1], self.nodes[ix][2] = front, back
			return ix
		return sub(0, cells)

	@classmethod
	def room(cls, fmt="v29", cells=8, subs=1, shape="balanced", vis="rle", see=1,
	         half=(512, 512, 256)):
		"""The base map. vis: "rle" (cell i sees cells i-see..i+see), "all"
		(every row all-visible, one shared row), "none" (no vis lump)."""
		m = cls(fmt)
		m.room_lo = [-h for h in half]
		m.room_hi = list(half)
		nhulls = 4 if m.halflife else 3

		# leafs: 0 = shared solid leaf, 1..cells = the room, then one empty
		# outside leaf per submodel (beyond visleafs, as qbsp writes them)
		m.leafs.append([CONTENTS_SOLID, -1, [0] * 3, [0] * 3, [0] * 4])
		step = (m.room_hi[0] - m.room_lo[0]) / cells
		for i in range(cells):
			lo = [m.room_lo[0] + step * i, m.room_lo[1], m.room_lo[2]]
			hi = [m.room_lo[0] + step * (i + 1), m.room_hi[1], m.room_hi[2]]
			m.leafs.append([CONTENTS_EMPTY, -1, lo, hi, [i & 255, (i * 7) & 255, 0, 255 if i == 0 else 0]])

		# world hull 0: room box, then the cells
		m.box_nodes(m.room_lo, m.room_hi, 0, leaf(0))
		# the last box node's inside child was a placeholder: the cells
		m.nodes[5][1] = m.cell_tree(m.room_lo[0], m.room_hi[0], cells, 1, shape)

		world_heads = [0]
		for h in range(1, nhulls):
			world_heads.append(m.room_clipnodes(m.room_lo, m.room_hi, h))

		m.models.append([list(m.room_lo), list(m.room_hi), [0, 0, 0], world_heads + [0] * (4 - nhulls), cells])

		ents = [b'{\n"classname" "worldspawn"\n"message" "bspfuzz synthetic"\n}\n',
		        b'{\n"classname" "info_player_deathmatch"\n"origin" "0 0 0"\n}\n']
		for s in range(subs):
			lo = [m.room_lo[0] + 64 + 96 * s, -32, m.room_lo[2]]
			hi = [lo[0] + 64, 32, m.room_lo[2] + 48]
			out_leaf = len(m.leafs)
			m.leafs.append([CONTENTS_EMPTY, -1, [0] * 3, [0] * 3, [0] * 4])
			heads = [m.box_nodes(lo, hi, leaf(0), leaf(out_leaf))]
			for h in range(1, nhulls):
				heads.append(m.box_clipnodes(lo, hi, h, CONTENTS_SOLID, CONTENTS_EMPTY))
			m.models.append([lo, hi, [0, 0, 0], heads + [0] * (4 - nhulls), 0])
			ents.append(b'{\n"classname" "func_door"\n"model" "*%d"\n}\n' % (s + 1))
		m.entities = b"".join(ents) + b"\0"

		if vis != "none":
			m.set_vis(see if vis == "rle" else None)
		return m

	def set_vis(self, see=None):
		"""PVS for leafs 1..visleafs: neighbours within `see` cells, or all
		visible (one row shared by every leaf) if see is None"""
		vl = self.models[0][4]
		rowlen = (vl + 7) >> 3
		if see is None:
			row = bytearray(b"\xff" * rowlen)
			if vl & 7:
				row[-1] = (1 << (vl & 7)) - 1
			self.vis = compress_row(bytes(row))
			for i in range(vl):
				self.leafs[1 + i][1] = 0
			return
		rows, data = {}, bytearray()
		for i in range(vl):
			row = bytearray(rowlen)
			for j in range(vl):
				if see is None or abs(i - j) <= see:
					row[j >> 3] |= 1 << (j & 7)
			c = compress_row(bytes(row))
			if c not in rows:
				rows[c] = len(data)
				data += c
			self.leafs[1 + i][1] = rows[c]
		self.vis = bytes(data)

	def pad_nodes(self, at, n):
		"""Insert n unreachable nodes before index `at`, renumbering children
		and headnodes, so the nodes after them get indices >= at + n."""
		self._insert(self.nodes, at, n, [0, leaf(0), leaf(0), [0] * 3, [0] * 3], hull=0)

	def pad_clipnodes(self, at, n):
		self._insert(self.clipnodes, at, n, [0, CONTENTS_SOLID, CONTENTS_SOLID], hull=None)

	def _insert(self, arr, at, n, filler, hull):
		for r in arr:
			for j in (1, 2):
				if r[j] >= at:
					r[j] += n
		hulls = [0] if hull == 0 else range(1, 4 if self.halflife else 3)
		for mdl in self.models:
			for h in hulls:
				if mdl[3][h] >= at:
					mdl[3][h] += n
		arr[at:at] = [list(filler) for _ in range(n)]

	# ------------------------------------------------------------ writing

	def lump_bytes(self):
		f = self.fmt
		small = f in ("v29", "hl")
		out = [b""] * 15
		out[LUMP_ENTITIES] = self.entities
		out[LUMP_PLANES] = b"".join(struct.pack("<4fi", *p) for p in self.planes)
		out[LUMP_VISIBILITY] = self.vis

		def bounds_s(v):
			return [max(-32768, min(32767, int(x))) for x in v]

		nodes = bytearray()
		for p, c0, c1, lo, hi in self.nodes:
			if small:
				nodes += struct.pack("<iHH3h3hHH", p, c0 & 0xffff, c1 & 0xffff, *bounds_s(lo), *bounds_s(hi), 0, 0)
			elif f == "2psb":
				nodes += struct.pack("<iii3h3hII", p, c0, c1, *bounds_s(lo), *bounds_s(hi), 0, 0)
			else:
				nodes += struct.pack("<iii3f3fII", p, c0, c1, *lo, *hi, 0, 0)
		out[LUMP_NODES] = bytes(nodes)

		clip = bytearray()
		for p, c0, c1 in self.clipnodes:
			clip += struct.pack("<iHH", p, c0 & 0xffff, c1 & 0xffff) if small else struct.pack("<iii", p, c0, c1)
		out[LUMP_CLIPNODES] = bytes(clip)

		leafs = bytearray()
		for c, v, lo, hi, amb in self.leafs:
			if small:
				leafs += struct.pack("<ii3h3hHH4B", c, v, *bounds_s(lo), *bounds_s(hi), 0, 0, *amb)
			elif f == "2psb":
				leafs += struct.pack("<ii3h3hII4B", c, v, *bounds_s(lo), *bounds_s(hi), 0, 0, *amb)
			else:
				leafs += struct.pack("<ii3f3fII4B", c, v, *lo, *hi, 0, 0, *amb)
		out[LUMP_LEAFS] = bytes(leafs)

		out[LUMP_MODELS] = b"".join(struct.pack("<9f4i3i", *lo, *hi, *org, *heads, vl, 0, 0)
		                            for lo, hi, org, heads, vl in self.models)
		for i, b in self.raw.items():
			out[i] = b
		return out

	def serialize(self):
		return build(self.fmt, self.lump_bytes(), self.place, self.bspx) + self.trailer

	def copy(self):
		import copy
		return copy.deepcopy(self)


def main():
	if len(sys.argv) != 2:
		sys.exit(__doc__)
	os.makedirs(sys.argv[1], exist_ok=True)
	for f in FORMATS:
		path = os.path.join(sys.argv[1], "base_%s.bsp" % f)
		data = Map.room(f).serialize()
		open(path, "wb").write(data)
		print("%s %d bytes" % (path, len(data)))


if __name__ == "__main__":
	main()
