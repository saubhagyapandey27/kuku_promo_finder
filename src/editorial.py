"""Shared editorial policy and stable family identity, independent of model prose."""
POLICY = '''A qualifying ending leaves a specific consequential story question inferable from
the selected content, whose answer or decisive resolution is absent by the cut. Danger,
a difficult choice, exposed identity/motive mystery, concrete interruption or unresolved
confrontation can qualify. A merely unseen reply, acknowledgment or reaction is insufficient.
A reveal can open an evidenced new question; an earlier payoff can precede a new dilemma.
Judge THIS question, not whether any earlier payoff occurred. Unknown names/titles/lore
are diagnostic, not automatic failures. Visual development and suspense can sustain silence.
Audience judges see only observed selected content; unspoken realization, hidden intent,
source annotations and future replies cannot supply missing in-cut facts. Script timing is
not word-level audio/frame alignment. All model scores are editorial heuristics.'''

HARD_VETO=frozenset(('PAYOFF_ALREADY_SHOWN','EMOTIONAL_STAKES_SETTLED',
    'NO_PENDING_ACTION_AT_CUT','HALLUCINATED_CORE_CONFLICT','MISSING_CAUSAL_TRIGGER_IN_CLIP',
    'INCOHERENT_SEGMENT_JUMP','UNANCHORED_PARTIAL_SHOT_SPOILER'))
NON_VETO=frozenset(('UNSTATED_CHARACTER_NAME','UNSTATED_TITLE_OR_AUTHORITY',
    'UNSTATED_PRIOR_BACKSTORY','MINOR_MOTIVATION_AMBIGUITY'))


def critic_approves(review):
    return (review.core_conflict_grounded is True and review.payoff_withheld_at_cut is True
            and not any(issue.code.value in HARD_VETO for issue in review.issues))


def critic_coverage_complete(expected, reviews):
    """Exactly one review for every configured critic, including a single authoritative seat."""
    keys=('model','model_id','family')
    identities=lambda rows: [tuple(row[k] for k in keys) for row in rows]
    wanted=identities(expected)
    actual=identities(reviews)
    return bool(wanted) and len(wanted)==len(set(wanted)) and sorted(actual)==sorted(wanted)


def participants(p, cfg):
    labels=set(p.judge_models.values())
    labels.update(p.editorial.get('generation_models',{}).values())
    labels.update(p.editorial.get(k) for k in ('viewer_model','audit_model'))
    for kind in ('jury_reviews',):
        labels.update(r.get('model') for r in p.editorial.get(kind,[]))
    labels.update(r.get('model') for r in p.editorial.get('readiness',{}).get('reviews',[]))
    return {cfg.spec(label.split(':')[0]).family for label in labels if label}
