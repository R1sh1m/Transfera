// ---------------------------------------------------------------------------
// Transfera v2 — KeepAwakeBanner
// Persistent, non-blocking reminder shown during device preview and transfer.
// Copy is honest: the phone's auto-lock cannot be disabled from this PC.
// ---------------------------------------------------------------------------

import { Smartphone } from "lucide-react";

export default function KeepAwakeBanner() {
  return (
    <div
      role="note"
      className="flex items-start gap-2 px-3 py-2 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg"
    >
      <Smartphone className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
      <p className="text-xs text-amber-700 dark:text-amber-300">
        Keep your iPhone unlocked — preview and transfer pause if it locks. Your
        phone&apos;s auto-lock can&apos;t be turned off from this PC, so tap the
        screen now and then to keep it awake.
      </p>
    </div>
  );
}
