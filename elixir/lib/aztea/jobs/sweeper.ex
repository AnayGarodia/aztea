defmodule Aztea.Jobs.Sweeper do
  @moduledoc """
  Periodic GenServer that scans for expired leases and recovers JobServers.

  Runs every `@sweep_interval_ms` (default 10s). On each tick:
    1. Resets lease-expired running jobs → pending (workers can re-claim)
    2. Ensures a JobServer exists for every active job (node-restart recovery)

  Timeout enforcement (timeout_seconds) is intentionally omitted here — it is
  handled by the Python sweeper in server/application_parts/part_006.py, which
  owns the authoritative timeout logic including retries and refunds.

  # INVARIANTS:
  # - Never touch payment or auth tables — Python owns those
  # - Lease reset is idempotent; concurrent Python sweeper runs are safe
  """

  use GenServer

  require Logger
  import Ecto.Query

  alias Aztea.Repo
  alias Aztea.Jobs.Schema, as: Job
  alias Aztea.Jobs.JobServer

  # Slower than Python (2s) to avoid racing on the same rows unnecessarily.
  @sweep_interval_ms 10_000
  @active_statuses ~w[pending running awaiting_clarification]

  def start_link(_opts), do: GenServer.start_link(__MODULE__, [], name: __MODULE__)

  @impl true
  def init(_) do
    schedule_sweep()
    {:ok, %{sweep_count: 0}}
  end

  @impl true
  def handle_info(:sweep, state) do
    sweep()
    schedule_sweep()
    {:noreply, %{state | sweep_count: state.sweep_count + 1}}
  end

  # ---------------------------------------------------------------------------

  defp sweep do
    now = DateTime.utc_now() |> DateTime.to_iso8601()

    # 1. Expire stale running leases → pending so a worker can reclaim.
    {expired_count, expired_jobs} =
      Repo.update_all(
        from(j in Job,
          where:
            j.status == "running" and
              not is_nil(j.lease_expires_at) and
              j.lease_expires_at < ^now,
          select: j.job_id
        ),
        set: [status: "pending", lease_expires_at: nil, updated_at: now]
      )

    if expired_count > 0 do
      Logger.info("sweeper.expired_leases count=#{expired_count} job_ids=#{inspect(expired_jobs)}")
    end

    # 2. Ensure a JobServer process exists for every active job.
    # This is the Elixir-specific recovery path after a node restart.
    active_jobs =
      Repo.all(
        from(j in Job,
          where: j.status in ^@active_statuses,
          select: j.job_id
        )
      )

    Enum.each(active_jobs, fn job_id ->
      case JobServer.start_or_find(job_id) do
        {:ok, _pid} ->
          :ok

        {:error, reason} ->
          Logger.warning(
            "sweeper.start_failed job_id=#{job_id} reason=#{inspect(reason)}"
          )
      end
    end)
  end

  defp schedule_sweep do
    Process.send_after(self(), :sweep, @sweep_interval_ms)
  end
end
