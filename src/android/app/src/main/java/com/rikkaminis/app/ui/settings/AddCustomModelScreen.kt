package com.rikkaminis.app.ui.settings

import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.res.stringResource
import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelEntry
import com.rikkaminis.app.data.repository.ProviderRepository
import com.rikkaminis.app.ui.components.RowLabel
import com.rikkaminis.app.ui.components.SectionTextField
import com.rikkaminis.app.R
import com.rikkaminis.app.ui.components.MinisButton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Add Custom Model — adopts the SettingsScaffold/SettingsSection toolkit
 * (T78). Single Identity section grouping the Model ID + Display Name
 * fields, with a footer caption explaining what the fields are for.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AddCustomModelScreen(
    instanceId: String,
    providerRepository: ProviderRepository,
    onBack: () -> Unit,
) {
    val instance = providerRepository.instance(instanceId)

    if (instance == null) {
        // [audit-0917] Navigate AFTER composition (see ModelGroupDetailScreen).
        LaunchedEffect(Unit) { onBack() }
        return
    }

    var modelId by remember { mutableStateOf("") }
    var displayName by remember { mutableStateOf("") }
    // [deep-audit-1033 I05] The write below is a coroutine now; without this
    // the Save button stays live for the round trip and a double tap can add
    // the same custom model twice (addEntry has no uniqueness check).
    var saving by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    SettingsScaffold(
        title = stringResource(R.string.provider_detail_add_custom_model),
        onBack = onBack,
    ) {
        SettingsSection(
            header = stringResource(R.string.add_provider_identity),
            footer = stringResource(R.string.add_custom_model_id_footer, instance.providerType.displayName),
        ) {
            SettingsCardBlock {
                RowLabel(text = stringResource(R.string.add_custom_model_model_id))
                SectionTextField(
                    value = modelId,
                    onValueChange = { modelId = it },
                    placeholder = stringResource(R.string.add_custom_model_eg_id_placeholder),
                    singleLine = true,
                    textStyle = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace),
                )
                Spacer(Modifier.height(12.dp))
                RowLabel(text = stringResource(R.string.add_custom_model_display_name_optional))
                SectionTextField(
                    value = displayName,
                    onValueChange = { displayName = it },
                    singleLine = true,
                )
            }
        }

        Spacer(Modifier.height(20.dp))
        MinisButton(
            onClick = {
                if (saving) return@MinisButton
                saving = true
                val name = displayName.ifBlank { modelId }
                val model = LLMModel(
                    id = modelId.trim(),
                    displayName = name.trim(),
                    provider = instance.providerType.displayName,
                )
                val entry = ModelEntry(
                    providerInstanceId = instanceId,
                    baseModel = model,
                    isCustom = true,
                )
                scope.launch {
                    try {
                        // [deep-audit-1033 I05] addEntry goes to SQLite on the
                        // caller's thread; saving a custom model used to block
                        // the frame that navigates back.
                        withContext(Dispatchers.IO) { providerRepository.addEntry(entry) }
                        onBack()
                    } finally {
                        saving = false
                    }
                }
            },
            enabled = modelId.isNotBlank() && !saving,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp),
        ) {
            Text(stringResource(R.string.common_save))
        }
        Spacer(Modifier.height(20.dp))
    }
}
