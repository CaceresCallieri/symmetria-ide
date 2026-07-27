cmake_minimum_required(VERSION 3.21)

project(symmetria-compositor VERSION 1.0 LANGUAGES CXX)

find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Quick WaylandCompositor)

qt_standard_project_setup(REQUIRES 6.5)

# A pure-plugin QML module: naming PLUGIN_TARGET the same as the target puts the
# registered types inside the plugin itself, so the installed module is one .so
# plus a qmldir — nothing for a consumer to link against, which is what we want
# since the only consumer is `import Symmetria.Compositor` from QML.
qt_add_qml_module(symmetriacompositor
    URI Symmetria.Compositor
    VERSION 1.0
    PLUGIN_TARGET symmetriacompositor
    OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/qml/Symmetria/Compositor
    SOURCES
        symmetriatrace.h
        symmetriacompositor.h
        symmetriacompositor.cpp
        symmetriaoutput.h
        symmetriaoutput.cpp
        symmetriashellsurfaceitem.h
        symmetriashellsurfaceitem.cpp
)

target_link_libraries(symmetriacompositor PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Quick
    Qt6::WaylandCompositor
)

# Where Qt itself keeps QML modules (/usr/lib/qt6/qml on Arch). Queried rather
# than hardcoded so the module lands next to QtWayland.Compositor on any
# prefix — a module installed anywhere else is invisible to the default import
# path, which fails BrowserPane.qml outright (QML has no conditional imports)
# and leaves the IDE with no browser surface at all.
if(NOT DEFINED QML_INSTALL_DIR)
    set(QML_INSTALL_DIR "${CMAKE_INSTALL_PREFIX}/lib/qt6/qml")
endif()

install(DIRECTORY "${CMAKE_BINARY_DIR}/qml/Symmetria"
        DESTINATION "${QML_INSTALL_DIR}")
