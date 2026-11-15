# Browser applications: migration to Codex

The Jobflow Browser Worker extension is retired. Use the reusable [Codex application workflow](CODEX_APPLICATIONS.md) from **Application batches → Open in Codex**.

Jobflow opens the desktop app with a prepared request in the current local project. Press **Send** once in Codex to start the batch. Codex can operate the actual employer controls, collect missing answers, and record observed submission confirmations. Opening the link alone does not start a run or submit an application.

Existing batches, saved materials, answers, application history, and uncertain results are retained. Preparing a Codex run clears obsolete extension review/authorization for the selected roles and reserves them for fresh Codex inspection. Completed and uncertain submissions are excluded from automatic retries.

New extension claims are disabled and the old download URL returns a retirement notice. You may stop or remove the old companion. Historical report and reconciliation operations remain available solely to finish recording an existing attempt safely. Check an uncertain employer result before trying again; never infer submission from a click or successful page load.

The website trigger currently requires the local Jobflow workspace and the Codex desktop app with browser controls available. Hosted-server paths are not sent as local workspace links. See [OpenAI's desktop deep-link reference](https://learn.chatgpt.com/docs/reference/commands#deep-links).
