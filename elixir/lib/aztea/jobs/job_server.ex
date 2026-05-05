defmodule Aztea.Jobs.JobServer do
  @moduledoc """
  GenServer that tracks a single job's lifecycle.

  One process per active job, supervised by Aztea.Jobs.Supervisor.
  The process exits normally when the job reaches a terminal state
  (complete | failed | cancelled).

  State machine transitions (mirrors Python server logic):
    pending → running (claim)
    running → complete | failed | cancelled (terminal)
    running → awaiting_clarification (clarification_request message)
    awaiting_clarification → running (clarification_response)
    running → pending (explicit release)

  The process holds an in-memory copy of the job row and syncs updates
  back to Postgres atomically. Job events are broadcast over Phoenix.PubSub
  so any connected subscriber (WebSocket, pipeline orchestrator) gets
  real-time status updates without polling.
  """

  use GenServer, restart: :transient

  require Logger
  import Ecto.Query

  alias Aztea.Repo
  alias Aztea.Jobs.Schema, as: Job

  # heartbeat_interval is not stored in the jobs table; use a conservative default.
  @default_heartbeat_interval_s 60
  @heartbeat_grace_seconds 30

  # ---------------------------------------------------------------------------
  # Public API
  # ---------------------------------------------------------------------------

  @doc "Start a job server for `job_id`. No-op if already started."
  def start_or_find(job_id) do
    case DynamicSupervisor.start_child(
           Aztea.Jobs.Supervisor,
           {__MODULE__, job_id}
         ) do
      {:ok, pid} -> {:ok, pid}
      {:error, {:already_started, pid}} -> {:ok, pid}
      err -> err
    end
  end

  @doc "Return the current in-memory job state."
  def get_state(job_id) do
    case find_pid(job_id) do
      {:ok, pid} -> GenServer.call(pid, :get_state)
      err -> err
    end
  end

  @doc "Record a heartbeat, extending the lease."
  def heartbeat(job_id) do
    with {:ok, pid} <- find_pid(job_id) do
      GenServer.call(pid, :heartbeat)
    end
  end

  @doc "Transition to a terminal state."
  def complete(job_id, result) do
    with {:ok, pid} <- find_pid(job_id) do
      GenServer.call(pid, {:complete, result})
    end
  end

  def fail(job_id, error) do
    with {:ok, pid} <- find_pid(job_id) do
      GenServer.call(pid, {:fail, error})
    end
  end

  def cancel(job_id) do
    with {:ok, pid} <- find_pid(job_id) do
      GenServer.call(pid, :cancel)
    end
  end

  # ---------------------------------------------------------------------------
  # GenServer callbacks
  # ---------------------------------------------------------------------------

  def start_link(job_id) do
    GenServer.start_link(__MODULE__, job_id, name: via(job_id))
  end

  @impl true
  def init(job_id) do
    case Repo.get(Job, job_id) do
      nil ->
        {:stop, {:error, :job_not_found}}

      job ->
        Logger.metadata(job_id: job_id)
        schedule_lease_check(job)
        {:ok, %{job: job, job_id: job_id}}
    end
  end

  @impl true
  def handle_call(:get_state, _from, state), do: {:reply, state.job, state}

  def handle_call(:heartbeat, _from, state) do
    new_expiry = utc_now_plus(@default_heartbeat_interval_s + @heartbeat_grace_seconds)

    {count, _} =
      Repo.update_all(
        from(j in Job, where: j.job_id == ^state.job_id and j.status == "running"),
        set: [lease_expires_at: new_expiry, updated_at: utc_now()]
      )

    if count == 1 do
      updated = %{state.job | lease_expires_at: new_expiry}
      broadcast(state.job_id, :heartbeat, %{expires_at: new_expiry})
      {:reply, :ok, %{state | job: updated}}
    else
      {:reply, {:error, :not_running}, state}
    end
  end

  def handle_call({:complete, result}, _from, state) do
    {:reply, result, apply_terminal(state, "complete", %{output_payload: result})}
  end

  def handle_call({:fail, error}, _from, state) do
    {:reply, :ok, apply_terminal(state, "failed", %{error_message: error})}
  end

  def handle_call(:cancel, _from, state) do
    {:reply, :ok, apply_terminal(state, "cancelled", %{})}
  end

  @impl true
  def handle_info(:check_lease, state) do
    now = utc_now()
    expiry = state.job.lease_expires_at || ""

    if state.job.status == "running" and expiry < now do
      Logger.warning("lease_expired", job_id: state.job_id)
      broadcast(state.job_id, :lease_expired, %{})
      # Transition back to pending so the Python sweeper (or another worker)
      # can reclaim it. The Python server remains the settlement authority.
      Repo.update_all(
        from(j in Job,
          where: j.job_id == ^state.job_id and j.status == "running"
        ),
        set: [status: "pending", updated_at: now, lease_expires_at: nil]
      )

      {:stop, :normal, state}
    else
      schedule_lease_check(state.job)
      {:noreply, state}
    end
  end

  # Deferred stop: sent by apply_terminal/3 so the caller's GenServer.call reply
  # is delivered before the process exits.
  @impl true
  def handle_info(:stop_after_terminal, state) do
    {:stop, :normal, state}
  end

  # Stop cleanly when job hits a terminal state.
  @impl true
  def terminate(reason, state) do
    Logger.info("job_server_stopping",
      job_id: state.job_id,
      status: state.job.status,
      reason: inspect(reason)
    )
  end

  # ---------------------------------------------------------------------------
  # Helpers
  # ---------------------------------------------------------------------------

  defp apply_terminal(state, status, fields) do
    updates = Map.merge(fields, %{status: status, updated_at: utc_now()})

    Repo.update_all(
      from(j in Job, where: j.job_id == ^state.job_id),
      set: Enum.to_list(updates)
    )

    updated = Map.merge(state.job, updates)
    broadcast(state.job_id, String.to_atom(status), updates)

    # Schedule self-exit after the next handle_info tick so callers get reply first.
    Process.send_after(self(), :stop_after_terminal, 0)
    %{state | job: updated}
  end

  defp schedule_lease_check(%Job{status: status}) when status == "running" do
    ms = :timer.seconds(@default_heartbeat_interval_s + @heartbeat_grace_seconds + 5)
    Process.send_after(self(), :check_lease, ms)
  end

  defp schedule_lease_check(_job), do: :ok

  defp broadcast(job_id, event, payload) do
    Phoenix.PubSub.broadcast(Aztea.PubSub, "job:#{job_id}", {event, payload})
  end

  defp via(job_id), do: {:via, Registry, {Aztea.Jobs.Registry, job_id}}

  defp find_pid(job_id) do
    case Registry.lookup(Aztea.Jobs.Registry, job_id) do
      [{pid, _}] -> {:ok, pid}
      [] -> {:error, :not_found}
    end
  end

  defp utc_now, do: DateTime.utc_now() |> DateTime.to_iso8601()

  defp utc_now_plus(seconds) do
    DateTime.utc_now()
    |> DateTime.add(seconds, :second)
    |> DateTime.to_iso8601()
  end
end
