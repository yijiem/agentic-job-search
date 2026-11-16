'use strict';

// Begin the private workspace read alongside UI downloads. app.js consumes this
// response once; a later manual refresh always requests current records.
if (!window.JobflowStateStarted) {
  window.JobflowStateStarted = true;
  window.JobflowInitialState = fetch('/api/state', {cache: 'no-store'}).then(
    response => ({response}),
    error => ({error})
  );
}
