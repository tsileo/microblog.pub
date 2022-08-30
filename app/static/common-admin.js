document.addEventListener('DOMContentLoaded', (ev) => {
  // Add confirm to "delete" button next to outbox objects
  var forms = document.getElementsByClassName("object-delete-form")
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener('submit', (ev) => {
      if (!confirm('Do you really want to delete this object?')) {
        ev.preventDefault();
      };
    });
  }
});
