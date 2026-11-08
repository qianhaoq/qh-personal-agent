package dev.qh.agent

import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.AndroidComposeTestRule
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import dev.qh.data.*
import kotlinx.coroutines.runBlocking
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.junit.rules.ExternalResource
import android.os.Looper
import org.robolectric.Shadows.shadowOf
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.LooperMode

/** Actual Compose layout/semantics on a host Android runtime. Not device or TalkBack QA. */
@OptIn(androidx.compose.ui.ExperimentalComposeUiApi::class)
@RunWith(RobolectricTestRunner::class)
// Match the actual window to the intended 320dp content, including separate dialog windows.
// Wider qualified dialog windows hit Robolectric #8460; 390dp/device coverage is unverified.
@Config(sdk = [34], qualifiers = "w320dp-h1600dp-mdpi")
@LooperMode(LooperMode.Mode.PAUSED)
class ComposeReviewTest {
    private lateinit var host: org.robolectric.android.controller.ActivityController<ComponentActivity>
    // The real Compose rule owns the recomposer, clock and content disposal. A controller
    // supplies the activity in both build variants without a debug-only manifest activity.
    @get:Rule val compose = AndroidComposeTestRule<ExternalResource, ComponentActivity>(
        object : ExternalResource() {
            override fun before() {
                host = Robolectric.buildActivity(ComponentActivity::class.java).setup().visible()
                // PAUSED mode does not execute posted startup/window traversal work itself.
                shadowOf(Looper.getMainLooper()).idle()
            }
            override fun after() {
                host.pause().stop().destroy()
                shadowOf(Looper.getMainLooper()).idle()
            }
        },
        { host.get() }
    )
    private fun content(scale: Float = 1f, block: @Composable () -> Unit) {
        assertEquals("Configured host width must match tested content", 320,
            host.get().resources.configuration.screenWidthDp)
        compose.setContent {
            MaterialTheme {
                CompositionLocalProvider(LocalDensity provides Density(1f, scale)) {
                    Box(Modifier.width(320.dp).fillMaxHeight().testTag("review-content")) { block() }
                }
            }
        }
        compose.waitForIdle()
        compose.runOnIdle {
            val activity = host.get()
            assertEquals("Actual host window width in dp", 320f,
                activity.window.decorView.width / activity.resources.displayMetrics.density, 0.01f)
        }
        compose.onNodeWithTag("review-content", useUnmergedTree = true).assertWidthIsEqualTo(320.dp)
    }
    private fun assertTargets() {
        val nodes = compose.onAllNodes(hasClickAction(), useUnmergedTree = true)
        assertTrue(nodes.fetchSemanticsNodes().isNotEmpty())
        for (index in nodes.fetchSemanticsNodes().indices) {
            val bounds = nodes[index].getUnclippedBoundsInRoot()
            assertTrue("Target width $bounds", bounds.right - bounds.left >= 48.dp)
            assertTrue("Target height $bounds", bounds.bottom - bounds.top >= 48.dp)
            val touch = nodes[index].fetchSemanticsNode().touchBoundsInRoot
            assertTrue("Touch bounds $touch", touch.width >= 48f && touch.height >= 48f)
        }
    }
    private fun fixture(): Pair<ReviewServer, ModelFixture> = runBlocking {
        val session = MockRepository(false, 0); session.login("demo")
        val server = ReviewServer(session)
        server to ModelFixture(server)
    }
    @Test fun material3AlreadyProvidesDefaultMinimumInteractiveBounds() {
        content { TextButton({}) { Text("默认 Material3") } }
        val touch = compose.onNodeWithText("默认 Material3").fetchSemanticsNode().touchBoundsInRoot
        assertTrue(touch.width >= 48f && touch.height >= 48f)
    }
    @Test fun allLocalResultsRenderServerConfirmationSeparately() {
        var result by mutableStateOf(LocalScheduleResult.UNKNOWN)
        content { Column { ReminderScheduleStatus("scheduled", result) } }
        LocalScheduleResult.entries.forEach { local ->
            compose.runOnIdle { result = local }
            compose.onNodeWithText("服务端已确认").assertExists()
            compose.onNodeWithText("本机：${local.label}").assertExists()
            compose.onAllNodes(hasText("已排程", substring = true)).assertCountEquals(0)
            if (!local.submitted) compose.onAllNodes(hasText("已提交", substring = true)).assertCountEquals(0)
        }
    }
    @Test fun realReminderAndAsrControlsHave48dpBoundsAtFontScale2() {
        val (_, f) = fixture()
        try {
            content(2f) {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    FlowReminderCard(f.model, ReminderDto("r", "n", "长提醒标题", "2099-01-01T00:00:00Z",
                        status = "scheduled", rev = 1), {}, { _, _ -> })
                    ListeningControls(AsrUiState.Listening, {}, {})
                }
            }
            assertTargets()
            compose.onNodeWithText("结束并识别").assertExists()
            compose.onNodeWithText("取消本次听写").assertExists()
        } finally { f.close() }
    }
    @Test fun recoveryDeletedHidesUpdateAndCopyAndRemovalDialogHas48dpBounds() {
        val request = noteDraft("A", "client-A")
        var recovery by mutableStateOf(CreateRecovery("op-A", request, "live", "nte-A"))
        content(2f) {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                RecoveryCard(recovery, Note("nte-A", "A", "organized", 3), "B", false, {}, {}, {}, {}, {})
            }
        }
        compose.onNodeWithText("将草稿另存为新记事，保留原记事").assertExists()
        assertTargets()
        compose.runOnIdle { recovery = recovery.copy(state = "deleted") }
        compose.onAllNodes(hasText("用当前草稿更新此记事")).assertCountEquals(0)
        compose.onAllNodes(hasText("将草稿另存为新记事，保留原记事")).assertCountEquals(0)
        compose.onNodeWithText("原记事不存在或已删除；下一草稿仍保留").assertExists()
        compose.onNodeWithText("移除本机恢复项，保留下一草稿").performScrollTo().performClick()
        compose.onNodeWithText("只移除本机恢复项？").assertExists()
        assertTargets()
    }
    @Test fun replayButtonUsesActualBusyAsrTtsArbitration() {
        var busy by mutableStateOf(true)
        var asr by mutableStateOf(AsrUiState.Idle)
        var tts by mutableStateOf(AnswerTtsState.Idle)
        var spoken: String? = null
        content {
            AnswerCard(AnswerItem("Q", answer = Answer("answer", emptyList())), busy, asr, tts,
                { spoken = it }, {}, {}, {}, {}, null)
        }
        compose.onNodeWithText("播报答案").assertIsNotEnabled()
        compose.runOnIdle { busy = false; asr = AsrUiState.Permission }
        compose.onNodeWithText("播报答案").assertIsNotEnabled()
        compose.runOnIdle { asr = AsrUiState.Idle }
        compose.onNodeWithText("播报答案").performClick()
        assertEquals("answer", spoken)
        compose.runOnIdle { tts = AnswerTtsState.Speaking }
        compose.onNodeWithText("正在播报").assertIsNotEnabled()
    }
    @Test fun confirmDialogUsesActual48dpActionsAtLargeFont() {
        checkConfirmationActions()
    }
    @Test
    @Config(sdk = [34], qualifiers = "w320dp-h640dp-mdpi")
    fun confirmDialogActionsRemainReachableOnShortPhoneAtLargeFont() {
        checkConfirmationActions()
    }
    private fun checkConfirmationActions() {
        var selectedTime: String? = null
        var confirmed = 0
        content(2f) {
            ReminderConfirmationDialog(ReminderDto("r", "n", "title", "2099-01-01T00:00:00Z",
                status = "suggested", rev = 1), false, "title", "2099-01-01T00:00:00Z", null,
                {}, { selectedTime = it }, {}, { confirmed++ })
        }
        assertTargets()
        compose.onNodeWithText("修改日期与时间").performScrollTo().performClick()
        val accept = compose.onNodeWithText("使用所选本地时间")
        accept.assertExists()
        // DatePicker's built-in day controls retain Material3's own semantics and sizing.
        accept.assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp).assertIsDisplayed()
        val cancel = compose.onAllNodesWithText("取消").onLast()
        cancel.assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp).assertIsDisplayed().performClick()
        compose.onNodeWithText("使用所选本地时间").assertDoesNotExist()
        assertNull(selectedTime)
        assertEquals(0, confirmed)
        compose.onNodeWithText("修改日期与时间").performScrollTo().performClick()
        // The body genuinely scrolls to time/zone while footer actions stay reachable.
        compose.onNode(hasText("时区：", substring = true)).performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("使用所选本地时间")
            .assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp).assertIsDisplayed().performClick()
        compose.onNodeWithText("使用所选本地时间").assertDoesNotExist()
        assertNotNull(selectedTime)
        assertTrue(java.time.Instant.parse(selectedTime).isAfter(java.time.Instant.now()))
        compose.onNodeWithText("确认", substring = false)
            .assertWidthIsAtLeast(48.dp).assertHeightIsAtLeast(48.dp).assertIsDisplayed().performClick()
        assertEquals(1, confirmed)
    }
    @Test fun fullAppNavigationFiltersSearchDetailAndDeleteAt320dpFontScale2() {
        val (server, f) = fixture()
        try {
            runBlocking { server.create(noteDraft("记事原文", "client-A"), server.captureOwner()) }
            content(2f) {
                AgentApp(f.model, { fallback, callback -> VoiceAsr(host.get(), callback, fallback) }, {}, {})
            }
            compose.onNodeWithText("详情").performScrollTo().performClick()
            assertTargets()
            compose.onNodeWithText("删除").performClick()
            compose.onNodeWithText("删除记事？").assertExists()
            assertTargets()
            compose.onNodeWithText("取消").performClick()
            compose.onNodeWithText("个人流").performClick()
            compose.onNodeWithText("搜索记忆").performClick()
            compose.onNodeWithText("搜索记忆（最多 500 字）").performTextInput("不存在")
            compose.onNodeWithText("搜索", substring = false).performClick()
            compose.onNodeWithText("没有找到相关记忆，可以试试更短的关键词。").assertExists()
            assertTargets()
            compose.onNodeWithText("个人流").performClick()
            compose.onAllNodes(hasText("提醒") and hasClickAction()).onFirst().performClick()
            compose.onNodeWithText("已结束").performClick()
            assertTargets()
        } finally { f.close() }
    }
}
