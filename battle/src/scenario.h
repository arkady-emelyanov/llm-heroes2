/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   Scenario files: a battle described in JSON, set up and fought without  *
 *   anyone touching the setup screen.                                      *
 *                                                                         *
 *   This is what makes the whole thing testable. With a scenario, both     *
 *   sides under non-human control and a dummy video driver, a battle runs  *
 *   start to finish unattended and prints a machine-readable result, which *
 *   is the unit of work an eval loop needs.                                *
 ***************************************************************************/

#pragma once

#include <string>

namespace Scenario
{
    // Runs the battle described by the JSON file at 'path'.
    //
    // Returns true if the battle was set up and fought. On failure, 'error' says what was wrong
    // with the scenario; a bad scenario is a mistake in the file, not something to paper over, so
    // nothing is defaulted silently.
    //
    // The result report is written to stdout as a single JSON line.
    bool run( const std::string & path, std::string & error );
}
