package com.rikkaminis.app.ui.chat

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController

/**
 * [fix/ime-overlay-focus-coverage] Dismisses the IME as soon as a window-owning
 * overlay opens.
 *
 * Why: `ModalBottomSheet` / `Dialog` install a focus restorer — they record the
 * focused node when they open and hand focus back when they dismiss. The
 * composer's TextField requests the IME the moment it regains focus, so an
 * overlay opened while the keyboard was up pops the keyboard back up on
 * dismiss. Clearing focus while the overlay is open leaves the restorer nothing
 * to hand back.
 *
 * Extracted so every overlay owner (ChatScreen's enumerated set, ChatInputArea's
 * MoveToSessionSheet) shares one implementation instead of one copy per host.
 *
 * ponytail: 只在打开时清焦点，不记录也不恢复用户原来的焦点。
 * 天花板: 用户关闭覆盖层后要继续输入需再点一次输入框（刻意取舍：宁可不弹键盘）。
 * 升级触发: 用户报告关闭覆盖层后想接着打字却要再点一次 → 记录原焦点并在关闭时恢复。
 */
@Composable
fun DismissImeWhileOverlayOpen(anyOverlayOpen: Boolean) {
    val keyboardController = LocalSoftwareKeyboardController.current
    val focusManager = LocalFocusManager.current
    LaunchedEffect(anyOverlayOpen) {
        if (anyOverlayOpen) {
            keyboardController?.hide()
            focusManager.clearFocus()
        }
    }
}
