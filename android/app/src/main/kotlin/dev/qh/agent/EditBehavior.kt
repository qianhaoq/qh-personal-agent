package dev.qh.agent

fun shouldPollOrganizeAfterEdit(status: String) = status == "uploaded"
fun shouldPopEditRoute(route: String?) = route == "edit"
