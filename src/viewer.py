"""Evidence-preserving, clip-only render. Partial visual prose is never assigned invented timing."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .render import humanize_heading,partial_visual_projection

VERSION = 'observed-question-viewer-7'
FACE = re.compile(r'\b(bearded|scarred|blond|blonde|red-haired|dark-haired|silver-haired|grey-haired|white-haired|blue-eyed|brown-eyed)\b', re.I)


@dataclass
class ViewerCut:
    text: str
    full_ids: set[str]
    partial_ids: set[str]
    content: dict[str, str] = field(default_factory=dict)
    uncertainty: dict[str, dict] = field(default_factory=dict)
    source: list[dict] = field(default_factory=list)
    timing_basis: dict[str,str] = field(default_factory=dict)


class ViewerRenderer:
    def __init__(self, show, scrub):
        self.show, self.scrub = show, scrub
        from .scrub import Scrubber,NameEntry
        # Alias resolution remains source-aware. Audience labels never inherit
        # episode-wide costume, status, relationship or secret-identity prose.
        self.audience_scrub=Scrubber([NameEntry(surfaces=e.surfaces,tags=[(1,9999,f'person {i+1}')])
            for i,e in enumerate(scrub.entries)],scrub.spoiler_terms,scrub.label_map,scrub.alias_confidence)

    def evidence(self, segments, first3s=False):
        out, full, partial, identities = [], set(), set(), {}
        content, uncertainty, source, timing = {}, {}, [], {}
        offset = 0.0
        limit = 3.0 if first3s else float('inf')

        def person(block,with_audio=True):
            label = block.speaker.upper().strip()
            entity = self.scrub.label_map.get((label, block.ep))
            tag = 'person'
            if self.scrub.alias_confidence.get((label,block.ep),1)<.8:
                entity=None
                tag='a person (source identity uncertain)'
            # Episode-wide bible wardrobe can lag the actual scene. Stable face
            # cues identify a speaker; selected source prose supplies clothing.
            face = frozenset(t.lower() for t in FACE.findall(block.speaker))
            if face:
                tag = 'the ' + ' '.join(sorted(face)) + ' person'
            # Reuse cross-label identity only with a shared visible face cue, not a hidden canonical name.
            key = ('label', label, entity, block.ep)
            if entity is not None and face:
                for oldkey, (_, oldentity, oldface) in identities.items():
                    if entity == oldentity and face & oldface:
                        key = oldkey
                        break
            if key not in identities:
                identities[key] = (f'V{len(identities)+1}', entity, face)
            marker=' (voiceover)' if re.search(r'(?<![A-Za-z])(?:V\.?\s*O\.?|voice[- ]?over)(?![A-Za-z])',block.label,re.I) else ' (off-screen)' if re.search(r'(?<![A-Za-z])(?:O\.?\s*S\.?|off[- ]?screen)(?![A-Za-z])',block.label,re.I) else ''
            if not with_audio: marker=''
            return marker + identities[key][0] + ' (' + tag + ')'

        selected = [b for seg in segments for b in self.show.episode(seg.ep).blocks
                    if b.kind == 'LINE' and b.t0 >= seg.start_s-.02 and b.t0 < seg.end_s]
        visual_people = {}
        for b in selected:
            visual_people.setdefault((b.ep,self.audience_scrub.speaker(b.speaker,b.ep)),set()).add(person(b,False))

        def picture(text, ep):
            text=re.sub(r'[,;]?\s*(?:the\s+)?(?:(?:sudden|dawning|haunting|horrifying|breathless|cold|full|terrible)\s+)*'
                r'(?:realiz(?:ation|es|ing)\b|recognition of\b|knowing that\b|secretly\b|intending to\b)[^.;]*',
                ' [unspoken inference omitted]', text, flags=re.I)
            # Map source names directly to the same anonymous visible actors as
            # speech. In particular, an episode-wide groom/prisoner tag must not
            # invent a costume change or a second person at an episode seam.
            aliases = {}
            replacements = {}
            for identity, entity, face in identities.values():
                if entity is None or not face:
                    continue
                entry = self.scrub.entries[entity]
                if not face.intersection(t.lower() for t in FACE.findall(entry.tag_for(ep))):
                    continue
                token = 'VISIBLEACTOR' + identity + 'TOKEN'
                replacements[token] = identity
                for alias in entry.surfaces:
                    aliases.setdefault(alias.lower(),token)
            if aliases:
                rx=re.compile(r'(?<!\w)('+'|'.join(re.escape(s) for s in sorted(aliases,key=lambda s:-len(s)))+r')(?!\w)',re.I)
                text=rx.sub(lambda m:aliases[m.group(0).lower()],text)
            text = self.audience_scrub.scrub(text,ep)
            for token,identity in replacements.items():
                text=text.replace(token,identity)
            for (tag_ep,tag), people in sorted(visual_people.items(),key=lambda kv:-len(kv[0][1])):
                if len(people)!=1 or tag_ep!=ep:
                    continue
                identity=next(iter(people))
                text = re.sub(r'(?<!\w)'+re.escape(tag)+r'(?!\w)',lambda _:identity,text,flags=re.I)
            return text

        for si, seg in enumerate(segments):
            ep, start, end = self.show.episode(seg.ep), seg.start_s, seg.end_s
            if offset >= limit:
                break
            if si:
                out.append('[CUT]')
            heading = next((sc.heading for sc in ep.scenes if sc.start <= start < sc.end), '')
            if not heading:
                heading = ep.scenes[0].heading if ep.scenes else ''
            out.append('SETTING: ' + humanize_heading(heading))
            visible_end = min(end, start + limit - offset)
            blocks = sorted((b for b in ep.blocks if b.t1 > start and b.t0 < visible_end),
                            key=lambda b: (max(start, b.t0), b.t0, b.kind, b.id))
            for b in blocks:
                a, z = max(start, b.t0), min(visible_end, b.t1)
                if b.kind == 'LINE' and b.t0 < start - .02:
                    continue
                complete = b.t0 >= start - .02 and b.t1 <= visible_end + .02
                source.append(b.model_dump())
                stamp = f'[{b.id} {offset+a-start:.2f}–{offset+z-start:.2f}s]'
                if complete:
                    full.add(b.id)
                    timing[b.id]='source-event-complete'
                    body = person(b) + ': "' + b.text + '"' if b.kind == 'LINE' else picture(b.text, b.ep)
                    content[b.id] = b.text if b.kind=='LINE' else body
                    out.append(f'{stamp} {b.kind}: {body}')
                elif b.kind == 'LINE' and first3s:
                    n = int(len(b.text.split()) * (z-a) / max(.001, b.t1-b.t0))
                    partial.add(b.id)
                    timing[b.id]='word-prefix-estimated'
                    uncertainty[b.id]={'timing_basis':'word-prefix-estimated','source_interval':[b.t0,b.t1], 'selected_interval':[a,z], 'asserted':False}
                    out.append(f'{stamp} source LINE ends at {offset+b.t1-start:.2f}s; audible prefix estimated, not aligned audio')
                    if n:
                        content[b.id]=' '.join(b.text.split()[:n])
                        out.append(f'{stamp} LINE (word timing estimated): {person(b)}: "' + ' '.join(b.text.split()[:n]) + '…"')
                else:
                    partial.add(b.id)
                    timing[b.id]='partial-visual-uncertain'
                    projection=partial_visual_projection(b,ep,start,visible_end)
                    anchored=[{'quote':picture(c['quote'],b.ep),'anchor_ids':c['anchor_ids']}
                              for c in projection['anchored_clauses']]
                    unknown=[{'quote':picture(c['quote'],b.ep),'possible_payoff':c['possible_payoff']}
                             for c in projection['unanchored_clauses']]
                    if anchored:
                        body=' '.join(c['quote'] for c in anchored)
                        content[b.id]=body
                        out.append(f'{stamp} {b.kind} (neutral framing/setup; sub-event timing uncertain): {body}')
                    if unknown:
                        uncertainty[b.id]={'kind':b.kind,'text':' '.join(c['quote'] for c in unknown),
                            'source_interval':[b.t0,b.t1],'selected_interval':[a,z],
                            'anchored_clauses':anchored,'unanchored_clauses':unknown,'asserted':False}
                        out.append(f'{stamp} {b.kind}: [unanchored action clauses; event timing unverified]')
            offset += end-start
        return ViewerCut('\n'.join(out), full, partial, content, uncertainty, source, timing)

    def render(self, segments, first3s=False, headers=True):
        return self.evidence(segments, first3s).text
