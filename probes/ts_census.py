#!/usr/bin/env python3
"""MPEG-TS conformance census over a RAW transport stream capture.

A decoder passing says almost nothing about the mux being spec-clean:
ffmpeg forgives continuity gaps, sloppy PCR, and lazy table cadence
that stricter consumers (hardware players, STBs, some NVRs) choke on.
This reads the actual bytes (ISO 13818-1) and asserts the numbers the
spec fixes. Over SRT the transport is reliable, so any continuity gap
in a capture is a mux bug, not network loss.

Emits "OK name -- detail" / "FAIL name -- detail" lines; exit 0 even
on FAILs (the caller tallies), 2 if the file is unusable.
"""
import sys

PKT = 188
SYNC = 0x47


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ts_census.py <raw.ts>", file=sys.stderr)
        return 2
    data = open(sys.argv[1], "rb").read()
    if len(data) < PKT * 100:
        print(f"FAIL ts capture usable -- only {len(data)} bytes")
        return 2

    # Align to the first sync byte that holds for 3 consecutive packets.
    start = -1
    for off in range(PKT):
        if all(data[off + i * PKT] == SYNC for i in range(3) if off + i * PKT < len(data)):
            start = off
            break
    if start < 0:
        print("FAIL ts sync -- no stable 0x47 alignment found")
        return 2

    npkts = (len(data) - start) // PKT
    bad_sync = 0
    cc_state = {}      # pid -> (last_cc, last_payload_bytes)
    cc_errors = 0
    cc_pids = set()
    pcr_samples = []   # (pkt_index, pcr_seconds)
    pat_at = []        # pkt indices of PAT starts
    pmt_at = []
    pmt_pid = -1
    pmt_streams = {}   # pid -> stream_type
    opus_reg = False
    pes_pids = set()
    pes_misaligned = 0
    pusi_seen = 0

    for n in range(npkts):
        p = data[start + n * PKT:start + (n + 1) * PKT]
        if p[0] != SYNC:
            bad_sync += 1
            continue
        pusi = (p[1] >> 6) & 1
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        cc = p[3] & 0x0F
        payload_off = 4
        if afc in (2, 3):
            aflen = p[4]
            payload_off = 5 + aflen
            if aflen >= 7 and (p[5] & 0x10):  # PCR flag
                base = (p[6] << 25) | (p[7] << 17) | (p[8] << 9) | (p[9] << 1) | (p[10] >> 7)
                ext = ((p[10] & 1) << 8) | p[11]
                pcr_samples.append((n, base / 90000.0 + ext / 27e6))

        if pid != 0x1FFF and afc in (1, 3):
            prev = cc_state.get(pid)
            if prev is not None:
                expect = (prev[0] + 1) & 0x0F
                if cc != expect:
                    # one identical duplicate is legal
                    if not (cc == prev[0] and p[4:] == prev[1]):
                        cc_errors += 1
            cc_state[pid] = (cc, p[4:])
            cc_pids.add(pid)

        if payload_off >= PKT:
            continue
        pl = p[payload_off:]

        if pid == 0 and pusi:  # PAT
            pat_at.append(n)
            ptr = pl[0]
            sec = pl[1 + ptr:]
            if len(sec) > 12 and sec[0] == 0x00:
                slen = ((sec[1] & 0x0F) << 8) | sec[2]
                progs = sec[8:3 + slen - 4]
                for i in range(0, len(progs) - 3, 4):
                    prog = (progs[i] << 8) | progs[i + 1]
                    if prog != 0:
                        pmt_pid = ((progs[i + 2] & 0x1F) << 8) | progs[i + 3]
        elif pid == pmt_pid and pusi:  # PMT
            pmt_at.append(n)
            ptr = pl[0]
            sec = pl[1 + ptr:]
            if len(sec) > 12 and sec[0] == 0x02:
                slen = ((sec[1] & 0x0F) << 8) | sec[2]
                pinfo = ((sec[10] & 0x0F) << 8) | sec[11]
                es = sec[12 + pinfo:3 + slen - 4]
                i = 0
                while i + 5 <= len(es):
                    stype = es[i]
                    spid = ((es[i + 1] & 0x1F) << 8) | es[i + 2]
                    eilen = ((es[i + 3] & 0x0F) << 8) | es[i + 4]
                    pmt_streams[spid] = stype
                    desc = es[i + 5:i + 5 + eilen]
                    j = 0
                    while j + 2 <= len(desc):
                        tag, dlen = desc[j], desc[j + 1]
                        if tag == 0x05 and desc[j + 2:j + 2 + 4] == b"Opus":
                            opus_reg = True
                        j += 2 + dlen
                    i += 5 + eilen
        elif pid in pmt_streams and pusi:
            pusi_seen += 1
            if pl[:3] != b"\x00\x00\x01":
                pes_misaligned += 1
            pes_pids.add(pid)

    def emit(okay, name, detail=""):
        print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""))

    emit(bad_sync == 0, "TS sync integrity",
         f"{npkts} packets" if bad_sync == 0 else f"{bad_sync}/{npkts} packets lost sync")

    emit(cc_errors == 0, "TS continuity counters clean",
         f"{len(cc_pids)} PIDs, {npkts} packets" if cc_errors == 0
         else f"{cc_errors} discontinuities (mux bug: SRT transport is reliable)")

    # Interpolate packet index -> time from PCR samples for cadence math.
    def t_of(idx):
        if not pcr_samples:
            return None
        for k in range(len(pcr_samples) - 1):
            a, b = pcr_samples[k], pcr_samples[k + 1]
            if a[0] <= idx <= b[0]:
                if b[0] == a[0]:
                    return a[1]
                return a[1] + (b[1] - a[1]) * (idx - a[0]) / (b[0] - a[0])
        return None

    if len(pcr_samples) >= 3:
        deltas = [(b[1] - a[1]) * 1000 for a, b in zip(pcr_samples, pcr_samples[1:])]
        mono = all(d > 0 for d in deltas)
        worst = max(deltas)
        emit(mono and worst <= 100.0, "TS PCR cadence (max 100ms, monotonic)",
             f"{len(pcr_samples)} PCRs, max gap {worst:.1f}ms")
    else:
        emit(False, "TS PCR cadence (max 100ms, monotonic)",
             f"only {len(pcr_samples)} PCR samples")

    for name, idxs in (("PAT", pat_at), ("PMT", pmt_at)):
        times = [t for t in (t_of(i) for i in idxs) if t is not None]
        if len(times) >= 2:
            worst = max((b - a) * 1000 for a, b in zip(times, times[1:]))
            emit(worst <= 500.0, f"TS {name} repetition (max 500ms)",
                 f"{len(idxs)} tables, max interval {worst:.1f}ms")
        else:
            emit(False, f"TS {name} repetition (max 500ms)", f"{len(idxs)} seen")

    types = sorted(set(pmt_streams.values()))
    has_v = any(t in (0x1B, 0x24) for t in types)
    has_a = any(t in (0x0F, 0x06, 0x03, 0x04) for t in types)
    emit(has_v, "TS PMT declares video", f"stream_types {[hex(t) for t in types]}")
    if has_a:
        if 0x06 in types:
            emit(opus_reg, "TS Opus registration descriptor",
                 "private stream carries 'Opus' tag" if opus_reg
                 else "stream_type 0x06 without Opus registration descriptor")
    else:
        emit(False, "TS PMT declares audio", f"stream_types {[hex(t) for t in types]}")

    emit(pes_misaligned == 0 and pusi_seen > 0, "TS PES starts aligned",
         f"{pusi_seen} PES starts" if pes_misaligned == 0
         else f"{pes_misaligned}/{pusi_seen} PUSI packets without 00 00 01")
    return 0


if __name__ == "__main__":
    sys.exit(main())
