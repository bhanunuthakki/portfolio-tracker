import { LatestBriefCard } from "@/components/LatestBriefCard";
import { PageHeader } from "@/components/ui";

/**
 * Monthly review — the ritual surface. Hosts the LLM-written monthly brief
 * (generate / browse past months / read). The same brief is emailed on the
 * first Saturday of each month (jobs.email_brief).
 */
export function Review(): JSX.Element {
  return (
    <div className="space-y-5">
      <PageHeader title="Monthly review" eyebrow="Ritual" />
      <LatestBriefCard />
    </div>
  );
}
