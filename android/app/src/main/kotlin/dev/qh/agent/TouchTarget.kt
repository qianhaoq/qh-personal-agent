package dev.qh.agent

import androidx.compose.foundation.layout.sizeIn
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/** Keep explicit visual/layout bounds as well as Material3's default minimum interactive semantics. */
internal fun Modifier.touchTarget() = sizeIn(minWidth = 48.dp, minHeight = 48.dp)
