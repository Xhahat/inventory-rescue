/** Full-screen wipe played when the user moves between screens: five colour bands sweep up in sequence. */
export default function Curtain() {
  return (
    <div className="lp-curtain" aria-hidden>
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="lp-strip" style={{ ['--i' as string]: i }} />
      ))}
      <span className="lp-curtain-mark">IR</span>
    </div>
  );
}
