package dev.qh.agent

// Pure state transitions also fence callbacks after cancellation / page disposal.
enum class AsrUiState { Idle, Permission, Listening, Processing }
enum class AsrEvent { RequestPermission, Granted, Denied, Start, Stop, Finish, Cancel }
fun reduceAsr(state: AsrUiState, event: AsrEvent): AsrUiState = when (event) {
    AsrEvent.RequestPermission -> if (state == AsrUiState.Idle) AsrUiState.Permission else state
    AsrEvent.Granted -> if (state == AsrUiState.Permission) AsrUiState.Listening else state
    AsrEvent.Start -> if (state == AsrUiState.Idle) AsrUiState.Listening else state
    AsrEvent.Stop -> if (state == AsrUiState.Listening) AsrUiState.Processing else state
    AsrEvent.Denied -> if (state == AsrUiState.Permission) AsrUiState.Idle else state
    AsrEvent.Finish, AsrEvent.Cancel -> AsrUiState.Idle
}
fun AsrUiState.acceptsResult() = this == AsrUiState.Listening || this == AsrUiState.Processing
