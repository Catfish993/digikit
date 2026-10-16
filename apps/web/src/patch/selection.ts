// Dependency and conflict reasoning for the gallery's selection, from the manifests alone (patchkit
// checks everything again when it applies). Pure functions, no DOM: node tests import this file.

export interface CardInfo {
  id: string;
  name: string;
  requires: string[];
  conflicts: string[];
  claims: string[];
}

const byId = (cards: CardInfo[]) => new Map(cards.map((c) => [c.id, c]));

/** The module with everything it needs (transitively), added to the selection. */
export function withRequired(cards: CardInfo[], selected: string[], id: string): string[] {
  const map = byId(cards);
  const out = [...selected];
  const visit = (one: string) => {
    if (out.includes(one)) return;
    out.push(one);
    for (const r of map.get(one)?.requires ?? []) visit(r);
  };
  visit(id);
  return out;
}

/** Removes a module and every selected module that needs it. */
export function withoutDependents(cards: CardInfo[], selected: string[], id: string): string[] {
  const map = byId(cards);
  const gone = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const s of selected)
      if (!gone.has(s) && (map.get(s)?.requires ?? []).some((r) => gone.has(r))) {
        gone.add(s);
        grew = true;
      }
  }
  return selected.filter((s) => !gone.has(s));
}

/** Why `a` and `b` cannot be applied together, or undefined. */
export function clash(a: CardInfo, b: CardInfo): string | undefined {
  if (a.conflicts.includes(b.id) || b.conflicts.includes(a.id))
    return `${a.name} conflicts with ${b.name}: one of them lists the other as a conflict.`;
  const shared = a.claims.filter((c) => b.claims.includes(c));
  if (shared.length)
    return `${a.name} and ${b.name} both use ${shared.map((s) => `"${s}"`).join(', ')}, which only one module can have.`;
  return undefined;
}

export interface Problems {
  /** Clashes inside the current selection (blocks Patch). */
  conflicts: string[];
  /** For modules not selected: why adding them (with what they need) would clash. */
  blockers: Map<string, string>;
}

export function selectionProblems(cards: CardInfo[], selected: string[]): Problems {
  const map = byId(cards);
  const chosen = selected.map((id) => map.get(id)).filter((c): c is CardInfo => Boolean(c));
  const conflicts: string[] = [];
  for (let i = 0; i < chosen.length; i++)
    for (let j = i + 1; j < chosen.length; j++) {
      const why = clash(chosen[i], chosen[j]);
      if (why) conflicts.push(why);
    }
  const blockers = new Map<string, string>();
  for (const card of cards) {
    if (selected.includes(card.id)) continue;
    const incoming = withRequired(cards, [], card.id)
      .map((id) => map.get(id))
      .filter((c): c is CardInfo => Boolean(c));
    outer: for (const add of incoming)
      for (const have of chosen) {
        const why = clash(add, have);
        if (why) {
          blockers.set(card.id, add.id === card.id ? why : `Needs ${add.name}. ${why}`);
          break outer;
        }
      }
  }
  return { conflicts, blockers };
}
