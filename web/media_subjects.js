/**
 * Which reference picture is evidence for which person.
 *
 * The document has held one block per person for a while -- `wardrobe` is A's,
 * `wardrobe#2` is B's -- but nothing said which FILE belonged to which of them,
 * so the build inferred it from the pixels on every run. Six pictures, three of
 * a woman and three of a man, is exactly where inference goes wrong: it either
 * invents six people or averages them into one.
 *
 * The binding lives on the asset (`asset.subject`), which survives a Reset and
 * a rebuild. A box with nothing in it yet exists only here, in `extra`, because
 * an empty box carries no information worth persisting.
 *
 * Letters are positional, never the stored index: delete the middle box and the
 * remaining two read A and B, with no renumbering write. `backend/conversation.
 * py:_reference_lines` assigns them the same way, and
 * `tests/test_frontend_parity.py` holds the two together.
 */

// Mirrors `backend/generic.py:MAX_PEOPLE`.
export const MAX_SUBJECTS = 6;

const LETTERS = ["A", "B", "C", "D", "E", "F"];

function storedIndex(asset) {
  const value = asset?.subject;
  return Number.isInteger(value) && value >= 1 && value <= MAX_SUBJECTS ? value : null;
}

/** Every box to draw: the ones pictures are in, plus the empty ones. */
export function subjectIndices(assets = [], extra = []) {
  const found = new Set();
  for (const asset of assets) {
    const index = storedIndex(asset);
    if (index !== null) found.add(index);
  }
  for (const index of extra) {
    if (Number.isInteger(index) && index >= 1 && index <= MAX_SUBJECTS) found.add(index);
  }
  return [...found].sort((a, b) => a - b);
}

/** The lowest unused slot, or null when there is no room for another person. */
export function nextSubjectIndex(indices = []) {
  const used = new Set(indices);
  for (let index = 1; index <= MAX_SUBJECTS; index += 1) {
    if (!used.has(index)) return index;
  }
  return null;
}

/** A, B, C by position in the list -- not by stored index. */
export function subjectLetter(indices, index) {
  const position = indices.indexOf(index);
  return position < 0 ? "" : LETTERS[position] || String(position + 1);
}

/**
 * Which of the six subject colours a box wears.
 *
 * By position, like the letter, so Subject A is always the first colour. The
 * colour is carried onto every card inside the box as well: mid-drag the box
 * chrome is the one thing you cannot see, and "which person is this picture"
 * is exactly the question being answered.
 */
export function subjectTone(indices, index) {
  const position = indices.indexOf(index);
  return position < 0 ? "" : `subject-${(position % LETTERS.length) + 1}`;
}

/**
 * The grouping that a box move produces, as ordered lists of asset ids.
 *
 * Box order IS person order -- the first box is person A and group 1 in the
 * prompt -- so reordering boxes has to be written in one pass. Done as a
 * sequence of single moves, two boxes would briefly share an index and merge
 * into one person.
 */
export function reorderedGroups(assets = [], indices = [], from = null, to = null) {
  const order = indices.filter((index) => assetsForSubject(assets, index).length);
  const start = order.indexOf(from);
  const end = order.indexOf(to);
  if (start < 0 || end < 0 || start === end) return null;
  const moved = [...order];
  moved.splice(end, 0, ...moved.splice(start, 1));
  return moved.map((index) => assetsForSubject(assets, index).map((asset) => asset.id));
}

export function assetsForSubject(assets = [], index) {
  return assets.filter((asset) => storedIndex(asset) === index);
}

/** Scene, style and wardrobe evidence: pictures that describe nobody. */
export function unassignedAssets(assets = []) {
  return assets.filter((asset) => storedIndex(asset) === null);
}

/**
 * Which empty boxes stay open after a picture moves.
 *
 * Dragging the last picture out of a box is re-sorting, not closing it: with
 * the box dropped on the spot, every box after it shifts up a letter mid-task
 * and the user never asked for that. So the box it left stays open and empty
 * until they close it themselves, while the box it landed in stops being
 * "extra" -- it holds a picture now.
 *
 * `from` is null when the box should simply be closed, which is what the
 * remove button wants.
 */
export function openBoxesAfterMove(open = [], { assets = [], into = null, from = null } = {}) {
  const result = new Set(open);
  result.delete(into);
  if (from !== null && from !== into && !assetsForSubject(assets, from).length) result.add(from);
  return [...result].sort((a, b) => a - b);
}

/**
 * Names, carried to wherever their box ended up.
 *
 * A name is stored against a person NUMBER, which is a box's position -- so
 * anything that shifts positions moves the names under them. Drag Bob's box
 * above Liz's and, without this, the box holding Bob's pictures is labelled
 * Liz, and the next build describes him by her name.
 *
 * Takes the filled box indices before and after the change. A box that is gone
 * takes its name with it; one that is new starts without one.
 */
export function remappedNames(previous = [], next = [], names = {}) {
  const result = {};
  next.forEach((index, position) => {
    const was = previous.indexOf(index);
    const name = was < 0 ? "" : names[String(was + 1)];
    if (typeof name === "string" && name.trim()) result[String(position + 1)] = name;
  });
  return result;
}

/**
 * Whether a card may be dropped into a subject box.
 *
 * Only a picture defines a person. A reference video or audio file stays in the
 * tray, which is the same rule `MediaStore.set_subject` enforces -- stated here
 * so the box can refuse the drop instead of the server rejecting it afterwards.
 */
export function canJoinSubject(asset) {
  return asset?.type === "image";
}
