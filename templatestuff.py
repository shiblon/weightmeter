from google.appengine.ext.webapp import template

register = template.create_template_register()

@register.filter
def stuff(value, arg):
  return "val: %s, arg: %s" % (value, arg)
