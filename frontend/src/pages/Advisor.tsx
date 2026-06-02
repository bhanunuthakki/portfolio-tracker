import { CIOChatCard } from "@/components/CIOChatCard";
import { PageHeader } from "@/components/ui";

/**
 * CIO advisor — the chat surface and the drill-down target for the cockpit.
 *
 * A cockpit item's "Discuss" link lands here as `/advisor?ask=<question>` with
 * the question pre-filled in the composer (start or pick a thread, then send).
 */
export function Advisor(): JSX.Element {
  return (
    <div className="space-y-5">
      <PageHeader title="CIO advisor" eyebrow="Discuss" />
      <CIOChatCard />
    </div>
  );
}
