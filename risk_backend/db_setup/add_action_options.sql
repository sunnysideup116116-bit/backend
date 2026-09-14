-- 2026-08-31 additive and repeatable MySQL/MariaDB migration.
ALTER TABLE `kb_interventions`
  ADD COLUMN IF NOT EXISTS `action_options` longtext
  CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL
  CHECK (`action_options` IS NULL OR json_valid(`action_options`));

UPDATE `kb_interventions`
SET `action_options` = '[{"action":"block_user","label":"封鎖"},{"action":"report_user","label":"檢舉"},{"action":"leave_conversation","label":"停止對話"}]'
WHERE `template_id` = 'restrict_receiver_options';

UPDATE `kb_interventions`
SET `action_options` = '[{"action":"dismiss","label":"繼續對話"},{"action":"block_user","label":"封鎖"},{"action":"report_user","label":"檢舉"},{"action":"leave_conversation","label":"結束對話"}]'
WHERE `template_id` = 'block_receiver_notice';

UPDATE `kb_interventions`
SET `action_type` = 'show_safety_info_card',
    `ui_behavior` = '{"show_options":true,"show_feedback_buttons":false,"allow_report_text":true,"mascot":"heart","display_throttle_seconds":300,"cooldown":0,"require_ack":false}',
    `action_options` = '[{"action":"block_user","label":"封鎖"},{"action":"report_user","label":"檢舉"},{"action":"leave_conversation","label":"停止對話"}]'
WHERE `template_id` = 'receiver_state_notice';
