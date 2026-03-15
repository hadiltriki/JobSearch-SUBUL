"use client";
import { useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";

function Redirect() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const userId = searchParams.get("user_id") || "";

  useEffect(() => {
    if (!userId) return;
    sessionStorage.setItem("jobscan_user_id", userId);

    async function check() {
      try {
        const hasJobsRes = await fetch(`/api/user/${userId}/has_jobs`);
        const hasJobsData = await hasJobsRes.json();
        if (hasJobsData.has_jobs) {
          router.replace(`/app?user_id=${userId}`);
        } else {
          router.replace(`/app?user_id=${userId}&scan=1`);
        }
      } catch {
        router.replace(`/app?user_id=${userId}&scan=1`);
      }
    }
    check();
  }, []);

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      Loading…
    </div>
  );
}

export default function JobsSearchPage() {
  return (
    <Suspense fallback={<div>Loading…</div>}>
      <Redirect />
    </Suspense>
  );
}