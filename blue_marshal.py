"""Pure-Python reader for CCP EVE "blue marshal" settings files.

Repo port of the corpus-validated research spike
``docs/superpowers/spikes/2026-07-09-probe-spike/blue_reader.py`` (which decoded
42/42 real settings files and all 34 in the overview recon cleanly, zero hard
opcodes). The decode logic is carried over verbatim from that spike — itself
ported from carbonengine/blue ``Marshal.h`` (opcode enum values) and
``Marshal.cpp`` (byte layouts + ReadHeader/ReadObject logic). Do not redesign
it; it is a faithful transcription.

Handles the subset of the format that appears in EVE client settings files
(``core_user_*.dat`` / ``core_char_*.dat``), plus opaque-but-traversable nodes
for GLOBAL / INSTANCE / REDUCE / NEWOBJ / LONG.

Hard opcodes (DBROW, WSTREAM, PICKLE, PICKLER) are detected and recorded; their
payloads cannot be length-skipped safely, so hitting one raises :class:`HardOpcode`
(recorded in ``reader.hard``, never a crash — a clean, catchable exception).

Opcode values are from Marshal.h; TY_SHAREDFLAG = 0x40, TY_TYPEMASK = 0x3F.

Public shape (stable, relied on by ``overview_dat``):

    obj, reader = load_file(path)
    reader.version    # 0 for all real settings files
    reader.mapcount   # v0 shared-slot count
    reader.hard       # Counter of hard opcodes hit
    reader.full_decode  # True iff the whole stream was consumed (pos == end)
"""
import struct
from collections import Counter

# ---- opcode enum (verbatim values from Marshal.h) ----
TY_INVALID    = 0
TY_SIGNATURE  = 126
TY_SIGNATURE2 = 125
TY_NONE       = 1
TY_GLOBAL     = 2
TY_INT64      = 3
TY_INT32      = 4
TY_INT16      = 5
TY_INT8       = 6
TY_INT_N1     = 7
TY_INT_0      = 8
TY_INT_1      = 9
TY_FLOAT      = 10
TY_FLOAT_0    = 11
TY_COMPLEX    = 12
TY_STR        = 13
TY_STR_EMPTY  = 14
TY_STR_CHAR   = 15
TY_STR_SHORT  = 16
TY_STR_TABLE  = 17
TY_UNICODE    = 18
TY_BUFFER     = 19
TY_TUPLE      = 20
TY_LIST       = 21
TY_DICT       = 22
TY_INSTANCE   = 23
TY_CALLBACK   = 25
TY_PICKLE     = 26
TY_REFERENCE  = 27
TY_CRC_CHECK  = 28
TY_TRUE       = 31
TY_FALSE      = 32
TY_PICKLER    = 33
TY_REDUCE     = 34
TY_NEWOBJ     = 35
TY_TUPLE0     = 36
TY_TUPLE1     = 37
TY_LIST0      = 38
TY_LIST1      = 39
TY_UNICODE_0  = 40
TY_UNICODE_1  = 41
TY_DBROW      = 42
TY_WSTREAM    = 43
TY_TUPLE2     = 44
TY_MARK       = 45
TY_UTF8_OBSOLETE = 46
TY_LONG       = 47
TY_SHAREDFLAG = 0x40
TY_TYPEMASK   = 0x3F

# name table for census reporting
_NAMES = {v: k for k, v in globals().items() if k.startswith("TY_") and isinstance(v, int)}


def opcode_name(t):
    return _NAMES.get(t & TY_TYPEMASK, "TY_?%d" % (t & TY_TYPEMASK))


# ---- shared string table (verbatim order from Marshal.cpp MARSHAL_STRINGS, 1-based index) ----
MARSHAL_STRINGS = [
 "*corpid","*locationid","age","Asteroid","authentication","ballID","beyonce","bloodlineID",
 "capacity","categoryID","character","characterID","characterName","characterType","charID","chatx",
 "clientID","config","contraband","corporationDateTime","corporationID","createDateTime","customInfo",
 "description","divisionID","DoDestinyUpdate","dogmaIM","EVE System","flag","foo.SlimItem","gangID",
 "Gemini","gender","graphicID","groupID","header","idName","invbroker","itemID","items","jumps","line",
 "lines","locationID","locationName","macho.CallReq","macho.CallRsp","macho.MachoAddress",
 "macho.Notification","macho.SessionChangeNotification","modules","name","objectCaching",
 "objectCaching.CachedObject","OnChatJoin","OnChatLeave","OnChatSpeak","OnGodmaShipEffect","OnItemChange",
 "OnModuleAttributeChange","OnMultiEvent","orbitID","ownerID","ownerName","quantity","raceID","RowClass",
 "securityStatus","Sentry Gun","sessionchange","singleton","skillEffect","squadronID","typeID","used",
 "userID","util.CachedObject","util.IndexRowset","util.Moniker","util.Row","util.Rowset","*multicastID",
 "AddBalls","AttackHit3","AttackHit3R","AttackHit4R","DoDestinyUpdates","GetLocationsEx",
 "InvalidateCachedObjects","JoinChannel","LSC","LaunchMissile","LeaveChannel","OID+","OID-",
 "OnAggressionChange","OnCharGangChange","OnCharNoLongerInStation","OnCharNowInStation","OnDamageMessage",
 "OnDamageStateChange","OnEffectHit","OnGangDamageStateChange","OnLSC","OnSpecialFX","OnTarget","RemoveBalls",
 "SendMessage","SetMaxSpeed","SetSpeedFraction","TerminalExplosion","address","alert","allianceID","allianceid",
 "bid","bookmark","bounty","channel","charid","constellationid","corpID","corpid","corprole","damage","duration",
 "effects.Laser","gangid","gangrole","hqID","issued","jit","languageID","locationid","machoVersion","marketProxy",
 "minVolume","orderID","price","range","regionID","regionid","role","rolesAtAll","rolesAtBase","rolesAtHQ",
 "rolesAtOther","shipid","sn","solarSystemID","solarsystemid","solarsystemid2","source","splash","stationID",
 "stationid","target","userType","userid","volEntered","volRemaining","weapon",
 "agent.missionTemplatizedContent_BasicKillMission","agent.missionTemplatizedContent_ResearchKillMission",
 "agent.missionTemplatizedContent_StorylineKillMission","agent.missionTemplatizedContent_GenericStorylineKillMission",
 "agent.missionTemplatizedContent_BasicCourierMission","agent.missionTemplatizedContent_ResearchCourierMission",
 "agent.missionTemplatizedContent_StorylineCourierMission","agent.missionTemplatizedContent_GenericStorylineCourierMission",
 "agent.missionTemplatizedContent_BasicTradeMission","agent.missionTemplatizedContent_ResearchTradeMission",
 "agent.missionTemplatizedContent_StorylineTradeMission","agent.missionTemplatizedContent_GenericStorylineTradeMission",
 "agent.offerTemplatizedContent_BasicExchangeOffer","agent.offerTemplatizedContent_BasicExchangeOffer_ContrabandDemand",
 "agent.offerTemplatizedContent_BasicExchangeOffer_Crafting","agent.LoyaltyPoints","agent.ResearchPoints",
 "agent.Credits","agent.Item","agent.Entity","agent.Objective","agent.FetchObjective","agent.EncounterObjective",
 "agent.DungeonObjective","agent.TransportObjective","agent.Reward","agent.TimeBonusReward","agent.MissionReferral",
 "agent.Location","agent.StandardMissionDetails","agent.OfferDetails","agent.ResearchMissionDetails",
 "agent.StorylineMissionDetails",
]


# ---- opaque node types (traversable but not natively instantiated) ----
class Global:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "Global(%r)" % self.name

    def __eq__(self, o):
        return isinstance(o, Global) and o.name == self.name

    def __hash__(self):
        return hash(("G", self.name))


class Instance:
    __slots__ = ("guid", "state")

    def __init__(self, guid, state):
        self.guid, self.state = guid, state

    def __repr__(self):
        return "Instance(guid=%r)" % (self.guid,)

    def __eq__(self, o):
        return isinstance(o, Instance) and o.guid == self.guid and o.state == self.state


class Reduce:
    __slots__ = ("rv", "listitems", "dictitems")

    def __init__(self, rv, li, di):
        self.rv, self.listitems, self.dictitems = rv, li, di

    def __repr__(self):
        return "Reduce(rv=%r)" % (self.rv,)

    def __eq__(self, o):
        return (isinstance(o, Reduce) and o.rv == self.rv
                and o.listitems == self.listitems and o.dictitems == self.dictitems)


class NewObj:
    __slots__ = ("rv", "listitems", "dictitems")

    def __init__(self, rv, li, di):
        self.rv, self.listitems, self.dictitems = rv, li, di

    def __repr__(self):
        return "NewObj(rv=%r)" % (self.rv,)

    def __eq__(self, o):
        return (isinstance(o, NewObj) and o.rv == self.rv
                and o.listitems == self.listitems and o.dictitems == self.dictitems)


class Buffer(bytes):
    """bytes subclass so buffers are distinguishable from decoded str keys but
    hashable as dict keys. ``isinstance(x, bytes)`` is True for a Buffer, and it
    hashes/compares equal to the plain ``bytes`` with the same contents, so
    ``d[b'overview']`` resolves a ``Buffer(b'overview')`` key transparently."""
    pass


class HardOpcode(Exception):
    def __init__(self, ty, pos):
        self.ty, self.pos = ty, pos
        super().__init__("Unsupported opcode %s at byte %d" % (opcode_name(ty), pos))


class BlueReader:
    def __init__(self, data, string_table=None):
        self.d = data
        self.n = len(data)
        self.pos = 0
        self.version = 0
        self.end = self.n            # effective end (shrinks past trailing map in v0)
        self.mapcount = 0
        self.mapping = None          # list[int] logical slot ids (v0)
        self.nshared = 0
        self.shared = []             # shared object table
        self.census = Counter()      # opcode -> count
        self.hard = Counter()        # hard opcode -> count (DBROW/WSTREAM/PICKLE/PICKLER)
        self.string_table = string_table or MARSHAL_STRINGS

    @property
    def full_decode(self):
        """True iff the object stream was fully consumed (pos reached the
        effective end — the trailing v0 map is excluded from ``end``)."""
        return self.pos == self.end

    # ---------- low level ----------
    def _u8(self):
        if self.pos >= self.end:
            raise EOFError("u8 past end @%d" % self.pos)
        v = self.d[self.pos]
        self.pos += 1
        return v

    def _s8(self):
        v = self._u8()
        return v - 256 if v >= 128 else v

    def _s16(self):
        v = struct.unpack_from("<h", self.d, self.pos)[0]
        self.pos += 2
        return v

    def _s32(self):
        v = struct.unpack_from("<i", self.d, self.pos)[0]
        self.pos += 4
        return v

    def _s64(self):
        v = struct.unpack_from("<q", self.d, self.pos)[0]
        self.pos += 8
        return v

    def _f64(self):
        v = struct.unpack_from("<d", self.d, self.pos)[0]
        self.pos += 8
        return v

    def _bytes(self, k):
        if self.pos + k > self.end:
            raise EOFError("bytes(%d) past end @%d" % (k, self.pos))
        b = self.d[self.pos:self.pos + k]
        self.pos += k
        return b

    def _readinteger(self):
        # Marshal.cpp ReadStream::ReadInteger: 1 byte; if 0xFF -> 4-byte int32 LE
        c = self._u8()
        if c == 0xFF:
            return self._s32()
        return c

    # ---------- shared table (mirrors MarkShared_Int) ----------
    def _mark_shared(self, obj):
        """Allocate a shared slot at the current declaration position; return slot index."""
        if self.version == 0:
            if self.nshared >= self.mapcount:
                raise ValueError("shared table overflow (v0)")
            ix = self.mapping[self.nshared] - 1
            self.nshared += 1
            self.shared[ix] = obj
            return ix
        else:
            ix = len(self.shared)
            self.shared.append(obj)
            return ix

    def _update_shared(self, ix, obj):
        self.shared[ix] = obj

    # ---------- header ----------
    def read_header(self):
        token = self._u8()
        if token not in (TY_SIGNATURE, TY_SIGNATURE2):
            raise ValueError("invalid marshal header 0x%02x" % token)
        if token == TY_SIGNATURE2:
            self.version = self._u8()
        else:
            self.version = 0
        if self.version == 0:
            self.mapcount = self._s32()
            if self.mapcount < 0:
                raise ValueError("invalid mapcount %d" % self.mapcount)
            if self.mapcount > 0:
                maplen = self.mapcount * 4
                if self.n - self.pos < maplen:
                    raise ValueError("stream too short for map")
                # mapping table lives at the very END of the buffer
                self.mapping = list(struct.unpack_from("<%di" % self.mapcount, self.d, self.n - maplen))
                for m in self.mapping:
                    if m < 1 or m > self.mapcount:
                        raise ValueError("bogus map entry %d" % m)
                self.shared = [None] * self.mapcount
                self.end = self.n - maplen   # don't parse the trailing map as object data
        else:
            self.mapcount = 0
            self.shared = []

    # ---------- main dispatch ----------
    def read_object(self):
        ty = self._u8()
        self.census[ty & TY_TYPEMASK] += 1
        shared = (ty & TY_SHAREDFLAG) == TY_SHAREDFLAG
        t = ty & TY_TYPEMASK
        h = self._handlers.get(t)
        if h is None:
            raise ValueError("Invalid type tag %d @%d" % (t, self.pos - 1))
        return h(self, shared)

    # ---------- scalar handlers ----------
    def _h_none(self, s):
        return None

    def _h_true(self, s):
        return True

    def _h_false(self, s):
        return False

    def _h_int_n1(self, s):
        return -1

    def _h_int_0(self, s):
        return 0

    def _h_int_1(self, s):
        return 1

    def _h_int8(self, s):
        return self._s8()

    def _h_int16(self, s):
        return self._s16()

    def _h_int32(self, s):
        return self._s32()

    def _h_int64(self, s):
        return self._s64()

    def _h_float_0(self, s):
        return 0.0

    def _h_float(self, s):
        return self._f64()

    def _decode_text(self, b):
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("latin-1")

    def _h_unicode(self, s):        # TY_STR, TY_UNICODE, TY_UTF8_OBSOLETE
        ln = self._readinteger()
        return self._decode_text(self._bytes(ln))

    def _h_unicode0(self, s):
        return ""

    def _h_unicode1(self, s):
        return self._decode_text(self._bytes(1))

    def _h_str_empty(self, s):
        return ""

    def _h_str_char(self, s):
        return self._decode_text(self._bytes(1))

    def _h_str_short(self, s):      # 1-byte length (NO 0xFF escape) + bytes
        ln = self._u8()
        return self._decode_text(self._bytes(ln))

    def _h_str_table(self, s):
        uc = self._u8()
        if uc < 1 or uc > len(self.string_table):
            raise ValueError("invalid string table index %d" % uc)
        return self.string_table[uc - 1]

    def _h_buffer(self, s):
        ln = self._readinteger()
        b = Buffer(self._bytes(ln))
        if s:
            self._mark_shared(b)
        return b

    def _h_long(self, s):
        ln = self._readinteger()
        b = self._bytes(ln)
        v = int.from_bytes(b, "little", signed=True) if ln else 0
        if s:
            self._mark_shared(v)
        return v

    # ---------- container handlers ----------
    def _h_tuple0(self, s):
        return ()

    def _h_list0(self, s):
        return []

    def _h_dict(self, s):
        ln = self._readinteger()
        d = {}
        if s:
            self._mark_shared(d)
        for _ in range(ln):
            val = self.read_object()   # value FIRST
            key = self.read_object()   # then key
            try:
                d[key] = val
            except TypeError:
                d[repr(key)] = val     # unhashable opaque key -> stringify (defensive; none in the settings corpus)
        return d

    def _h_list(self, s):
        ln = self._readinteger()
        lst = []
        if s:
            self._mark_shared(lst)
        for _ in range(ln):
            lst.append(self.read_object())
        return lst

    def _h_list1(self, s):
        lst = []
        if s:
            self._mark_shared(lst)
        lst.append(self.read_object())
        return lst

    def _h_tuple(self, s):
        ln = self._readinteger()
        if s:
            ix = self._mark_shared(None)
            items = [self.read_object() for _ in range(ln)]
            t = tuple(items)
            self._update_shared(ix, t)
            return t
        return tuple(self.read_object() for _ in range(ln))

    def _h_tuple1(self, s):
        if s:
            ix = self._mark_shared(None)
            t = (self.read_object(),)
            self._update_shared(ix, t)
            return t
        return (self.read_object(),)

    def _h_tuple2(self, s):
        if s:
            ix = self._mark_shared(None)
            a = self.read_object()
            b = self.read_object()
            t = (a, b)
            self._update_shared(ix, t)
            return t
        a = self.read_object()
        b = self.read_object()
        return (a, b)

    # ---------- reference ----------
    def _h_reference(self, s):
        idx = self._readinteger()
        if self.version == 0:
            if idx < 1 or idx > self.mapcount:
                raise ValueError("invalid TY_REFERENCE %d (v0)" % idx)
            return self.shared[idx - 1]
        else:
            if idx < 0 or idx >= len(self.shared):
                raise ValueError("invalid TY_REFERENCE %d (v1)" % idx)
            return self.shared[idx]

    # ---------- crc ----------
    def _h_crc(self, s):
        self._s32()          # crc value (not verified in spike)
        return self.read_object()

    # ---------- opaque globals / instances / reduce / newobj ----------
    def _h_global(self, s):
        ln = self._readinteger()
        name = self._decode_text(self._bytes(ln))
        g = Global(name)
        if s:
            self._mark_shared(g)
        return g

    def _h_instance(self, s):
        ix = self._mark_shared(None) if s else None
        guid = self.read_object()
        inst = Instance(guid, None)
        if s:
            self._update_shared(ix, inst)
        state = self.read_object()
        inst.state = state
        return inst

    def _read_listiter(self):
        items = []
        while True:
            if self._peek_type() == TY_MARK:
                self.census[TY_MARK] += 1
                self.pos += 1
                break
            items.append(self.read_object())
        return items

    def _read_dictiter(self):
        items = []
        while True:
            if self._peek_type() == TY_MARK:
                self.census[TY_MARK] += 1
                self.pos += 1
                break
            k = self.read_object()
            v = self.read_object()
            items.append((k, v))
        return items

    def _peek_type(self):
        return self.d[self.pos] & TY_TYPEMASK

    def _h_reduce(self, s):
        ix = self._mark_shared(None) if s else None
        rv = self.read_object()
        obj = Reduce(rv, None, None)
        if s:
            self._update_shared(ix, obj)
        obj.listitems = self._read_listiter()
        obj.dictitems = self._read_dictiter()
        return obj

    def _h_newobj(self, s):
        ix = self._mark_shared(None) if s else None
        rv = self.read_object()
        obj = NewObj(rv, None, None)
        if s:
            self._update_shared(ix, obj)
        obj.listitems = self._read_listiter()
        obj.dictitems = self._read_dictiter()
        return obj

    def _h_callback(self, s):
        return self.read_object()

    # ---------- hard opcodes (record + abort) ----------
    def _h_dbrow(self, s):
        self.hard[TY_DBROW] += 1
        raise HardOpcode(TY_DBROW, self.pos - 1)

    def _h_wstream(self, s):
        self.hard[TY_WSTREAM] += 1
        raise HardOpcode(TY_WSTREAM, self.pos - 1)

    def _h_pickle(self, s):
        self.hard[TY_PICKLE] += 1
        raise HardOpcode(TY_PICKLE, self.pos - 1)

    def _h_pickler(self, s):
        self.hard[TY_PICKLER] += 1
        raise HardOpcode(TY_PICKLER, self.pos - 1)

    def load(self):
        self.read_header()
        return self.read_object()


BlueReader._handlers = {
    TY_NONE: BlueReader._h_none, TY_TRUE: BlueReader._h_true, TY_FALSE: BlueReader._h_false,
    TY_INT_N1: BlueReader._h_int_n1, TY_INT_0: BlueReader._h_int_0, TY_INT_1: BlueReader._h_int_1,
    TY_INT8: BlueReader._h_int8, TY_INT16: BlueReader._h_int16, TY_INT32: BlueReader._h_int32,
    TY_INT64: BlueReader._h_int64, TY_FLOAT_0: BlueReader._h_float_0, TY_FLOAT: BlueReader._h_float,
    TY_STR: BlueReader._h_unicode, TY_UNICODE: BlueReader._h_unicode, TY_UTF8_OBSOLETE: BlueReader._h_unicode,
    TY_UNICODE_0: BlueReader._h_unicode0, TY_UNICODE_1: BlueReader._h_unicode1,
    TY_STR_EMPTY: BlueReader._h_str_empty, TY_STR_CHAR: BlueReader._h_str_char,
    TY_STR_SHORT: BlueReader._h_str_short, TY_STR_TABLE: BlueReader._h_str_table,
    TY_BUFFER: BlueReader._h_buffer, TY_LONG: BlueReader._h_long,
    TY_TUPLE0: BlueReader._h_tuple0, TY_LIST0: BlueReader._h_list0,
    TY_DICT: BlueReader._h_dict, TY_LIST: BlueReader._h_list, TY_LIST1: BlueReader._h_list1,
    TY_TUPLE: BlueReader._h_tuple, TY_TUPLE1: BlueReader._h_tuple1, TY_TUPLE2: BlueReader._h_tuple2,
    TY_REFERENCE: BlueReader._h_reference, TY_CRC_CHECK: BlueReader._h_crc,
    TY_GLOBAL: BlueReader._h_global, TY_INSTANCE: BlueReader._h_instance,
    TY_REDUCE: BlueReader._h_reduce, TY_NEWOBJ: BlueReader._h_newobj, TY_CALLBACK: BlueReader._h_callback,
    TY_DBROW: BlueReader._h_dbrow, TY_WSTREAM: BlueReader._h_wstream,
    TY_PICKLE: BlueReader._h_pickle, TY_PICKLER: BlueReader._h_pickler,
}


def load_file(path, string_table=None):
    """Decode a blue-marshal file. Returns ``(obj, reader)``.

    Raises :class:`HardOpcode` if the stream contains an unsupported opcode
    (DBROW/WSTREAM/PICKLE/PICKLER) — the exception carries ``.ty``/``.pos`` and
    the count is also recorded on the reader that was building the object. Other
    malformed input raises ``ValueError``/``EOFError``.
    """
    with open(path, "rb") as f:
        data = f.read()
    return loads(data, string_table=string_table)


def loads(data, string_table=None):
    """Decode blue-marshal ``bytes``. Returns ``(obj, reader)``."""
    r = BlueReader(data, string_table=string_table)
    obj = r.load()
    return obj, r


if __name__ == "__main__":
    import sys
    obj, r = load_file(sys.argv[1])
    print("version", r.version, "mapcount", r.mapcount, "top-type", type(obj).__name__,
          "keys" if isinstance(obj, dict) else "", len(obj) if hasattr(obj, "__len__") else "",
          "full_decode", r.full_decode)
    print("census:", {opcode_name(k): v for k, v in sorted(r.census.items())})
    print("hard:", {opcode_name(k): v for k, v in r.hard.items()})
