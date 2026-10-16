// Scroll timeline shared by the 3D scene and the page (kept tiny so the page never pulls in the 3D chunk).
export const TOTAL_LOAD = 54;
export const LOAD_START = 0.54;
export const LOAD_LAST = 0.745;
export const LOAD_SPAN = 0.06;
export const LOAD_STEP = (LOAD_LAST - LOAD_START) / (TOTAL_LOAD - 1);

export function loadedCount(p: number): number {
  if (p < LOAD_START + LOAD_SPAN) return 0;
  return Math.min(TOTAL_LOAD, Math.floor((p - LOAD_START - LOAD_SPAN) / LOAD_STEP) + 1);
}

/** Scroll progress where each story page rests (Start is the top of the page). One wheel step moves to the next. */
export const STOPS = [0.255, 0.44, 0.68, 0.92] as const;

/** Fade windows [in start, in end, out start, out end] for each story chapter. */
export const CHAPTERS: readonly (readonly [number, number, number, number])[] = [
  [-1, 0, 0.1, 0.16],
  [0.17, 0.21, 0.3, 0.34],
  [0.35, 0.39, 0.49, 0.53],
  [0.545, 0.58, 0.78, 0.81],
  [0.83, 0.87, 0.97, 1.01],
];
