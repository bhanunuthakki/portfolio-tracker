import { useMemo, useState } from "react";

/**
 * Hook + helpers for sortable tables with search filtering.
 *
 * Usage:
 *
 *   const { sortKey, sortDir, sortedRows, headerProps } = useTableSort(
 *     rows,
 *     "date",            // default sort key
 *     "desc",            // default direction
 *     {
 *       date: (r) => r.date,
 *       amount: (r) => parseFloat(r.amount),
 *       account: (r) => r.account_name,
 *     },
 *   );
 *
 *   // In your <th>:
 *   <th {...headerProps("date", "Date")}>Date</th>
 *
 *   // Or for finer control:
 *   <th onClick={() => toggle("date")}>
 *     Date {sortIndicator("date")}
 *   </th>
 */

export type SortDir = "asc" | "desc";

export type Accessor<T> = (row: T) => string | number | null | undefined;

export interface UseTableSortResult<T> {
  sortKey: string;
  sortDir: SortDir;
  sortedRows: T[];
  toggle: (key: string) => void;
  sortIndicator: (key: string) => string;
}

export function useTableSort<T>(
  rows: T[],
  defaultKey: string,
  defaultDir: SortDir = "desc",
  accessors: Record<string, Accessor<T>>,
): UseTableSortResult<T> {
  const [sortKey, setSortKey] = useState<string>(defaultKey);
  const [sortDir, setSortDir] = useState<SortDir>(defaultDir);

  const sortedRows = useMemo(() => {
    const acc = accessors[sortKey];
    if (!acc) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const va = acc(a);
      const vb = acc(b);
      // Push nulls/undefined to the end regardless of direction
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      let cmp: number;
      if (typeof va === "number" && typeof vb === "number") {
        cmp = va - vb;
      } else {
        cmp = String(va).localeCompare(String(vb));
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortKey, sortDir, accessors]);

  const toggle = (key: string) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Default to descending for numerics, ascending for everything else.
      // Users almost always want "biggest first" on a $ column.
      setSortDir("desc");
    }
  };

  const sortIndicator = (key: string): string => {
    if (key !== sortKey) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
  };

  return { sortKey, sortDir, sortedRows, toggle, sortIndicator };
}

/**
 * Filter rows by a free-text search against an array of accessors. Empty
 * query returns the full list. Match is case-insensitive substring across
 * any field.
 */
export function filterBySearch<T>(
  rows: T[],
  query: string,
  fields: Accessor<T>[],
): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((row) =>
    fields.some((f) => {
      const v = f(row);
      if (v == null) return false;
      return String(v).toLowerCase().includes(q);
    }),
  );
}
