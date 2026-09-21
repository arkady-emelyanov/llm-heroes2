/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   Loads a battle from a JSON file and fights it. See scenario.h.        *
 *                                                                         *
 *   The setup mirrors what Battle::Only::StartBattle does, deliberately    *
 *   rather than reusing it: Battle::Only owns the setup screen's state and *
 *   exposes none of it, and reproducing five lines of player bookkeeping   *
 *   is cheaper than vendoring its header to get at them.                   *
 ***************************************************************************/

#include "scenario.h"

#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "army.h"
#include "army_troop.h"
#include "artifact.h"
#include "battle.h"
#include "external_ai.h"
#include "game.h"
#include "ground.h"
#include "heroes.h"
#include "json.h"
#include "monster.h"
#include "spell.h"
#include "spell_storage.h"
#include "players.h"
#include "settings.h"
#include "tools.h"
#include "world.h"

namespace
{
    // The setup screen colours the attacker blue and the defender red; scenarios keep that mapping
    // so that the two ways of starting a battle describe the same thing.
    const PlayerColor sideColor[2]{ PlayerColor::BLUE, PlayerColor::RED };

    const char * const sideKey[2]{ "blue", "red" };

    bool readFile( const std::string & path, std::string & contents, std::string & error )
    {
        std::ifstream file( path );

        if ( !file ) {
            error = "cannot open '" + path + "'";
            return false;
        }

        std::ostringstream buffer;
        buffer << file.rdbuf();

        contents = buffer.str();

        return true;
    }

    // Scenarios name things the way the game does, so that a file is readable without a lookup
    // table. Matching is case-insensitive because "Lord Kilburn" and "lord kilburn" are the same
    // hero to everyone but a string comparison.
    std::string normalise( const std::string & name )
    {
        std::string result;
        result.reserve( name.size() );

        for ( const char c : name ) {
            if ( c != ' ' && c != '_' && c != '-' ) {
                result += static_cast<char>( std::tolower( static_cast<unsigned char>( c ) ) );
            }
        }

        return result;
    }

    int findHeroByName( const std::string & name )
    {
        const std::string wanted = normalise( name );

        for ( int id = Heroes::UNKNOWN + 1; id < Heroes::HEROES_COUNT; ++id ) {
            const Heroes * hero = world.GetHeroes( id );

            if ( hero != nullptr && normalise( hero->GetName() ) == wanted ) {
                return id;
            }
        }

        return Heroes::UNKNOWN;
    }

    int findMonsterByName( const std::string & name )
    {
        const std::string wanted = normalise( name );

        for ( int id = Monster::UNKNOWN + 1; id < Monster::MONSTER_COUNT; ++id ) {
            const Monster monster( id );

            if ( monster.isValid() && normalise( monster.GetName() ) == wanted ) {
                return id;
            }
        }

        return Monster::UNKNOWN;
    }

    int findSpellByName( const std::string & name )
    {
        const std::string wanted = normalise( name );

        for ( int id = Spell::NONE + 1; id < Spell::SPELL_COUNT; ++id ) {
            const Spell spell( id );

            if ( spell.isValid() && normalise( spell.GetName() ) == wanted ) {
                return id;
            }
        }

        return Spell::NONE;
    }

    // Gives a hero the listed spells, creating a spell book if the faction did not start with one.
    // Knights and Barbarians have no initial spell and therefore no book at all, so without this a
    // scenario could not give them magic.
    bool giveSpells( const Json::Value & spells, Heroes & hero, const std::string & key, std::string & error )
    {
        if ( !spells.isArray() ) {
            error = "'" + key + "': \"spells\" must be an array of spell names";
            return false;
        }

        if ( spells.items().empty() ) {
            return true;
        }

        if ( !hero.HaveSpellBook() && !hero.SpellBookActivate() ) {
            error = "'" + key + "': " + hero.GetName() + " cannot be given a spell book";
            return false;
        }

        for ( const Json::Value & entry : spells.items() ) {
            if ( !entry.isString() ) {
                error = "'" + key + "': every entry of \"spells\" must be a spell name";
                return false;
            }

            const int spellId = findSpellByName( entry.asString() );
            if ( spellId == Spell::NONE ) {
                error = "'" + key + "': there is no spell called '" + entry.asString() + "'";
                return false;
            }

            // withoutWisdom: a scenario says what the hero knows, so it is not for the Wisdom
            // skill to veto it.
            hero.AppendSpellToBook( Spell( spellId ), true );
        }

        return true;
    }

    int findArtifactByName( const std::string & name )
    {
        const std::string wanted = normalise( name );

        for ( int id = Artifact::UNKNOWN + 1; id < Artifact::ARTIFACT_COUNT; ++id ) {
            const Artifact artifact( id );

            if ( artifact.isValid() && normalise( artifact.GetName() ) == wanted ) {
                return id;
            }
        }

        return Artifact::UNKNOWN;
    }

    // Puts the listed artifacts in the hero's bag. Artifacts change the battle through the primary
    // skills and spell effects they grant, so they are worth being able to set up deliberately.
    bool giveArtifacts( const Json::Value & artifacts, Heroes & hero, const std::string & key, std::string & error )
    {
        if ( !artifacts.isArray() ) {
            error = "'" + key + "': \"artifacts\" must be an array of artifact names";
            return false;
        }

        for ( const Json::Value & entry : artifacts.items() ) {
            if ( !entry.isString() ) {
                error = "'" + key + "': every entry of \"artifacts\" must be an artifact name";
                return false;
            }

            const int artifactId = findArtifactByName( entry.asString() );
            if ( artifactId == Artifact::UNKNOWN ) {
                error = "'" + key + "': there is no artifact called '" + entry.asString() + "'";
                return false;
            }

            // A hero whose faction starts with magic already carries a spell book, and a second one
            // is refused. Asking for the book they already have is a harmless way to write "this
            // hero can cast", so it is not treated as a mistake.
            if ( artifactId == Artifact::MAGIC_BOOK && hero.HaveSpellBook() ) {
                continue;
            }

            // The bag is finite, so a scenario asking for more than fits is a mistake worth naming
            // rather than quietly dropping the overflow.
            if ( !hero.GetBagArtifacts().PushArtifact( Artifact( artifactId ) ) ) {
                error = "'" + key + "': " + hero.GetName() + " cannot carry '" + entry.asString() + "'; the artifact bag is full";
                return false;
            }
        }

        return true;
    }

    int findTerrainByName( const std::string & name )
    {
        const std::string wanted = normalise( name );

        const struct
        {
            const char * name;
            int ground;
        } terrains[] = { { "desert", Maps::Ground::DESERT }, { "snow", Maps::Ground::SNOW },     { "swamp", Maps::Ground::SWAMP },
                         { "wasteland", Maps::Ground::WASTELAND }, { "beach", Maps::Ground::BEACH }, { "lava", Maps::Ground::LAVA },
                         { "dirt", Maps::Ground::DIRT }, { "grass", Maps::Ground::GRASS },       { "water", Maps::Ground::WATER } };

        for ( const auto & terrain : terrains ) {
            if ( wanted == terrain.name ) {
                return terrain.ground;
            }
        }

        return Maps::Ground::UNKNOWN;
    }

    // Fills one side's hero and army from its scenario entry.
    bool buildSide( const Json::Value & side, const int index, Heroes *& hero, std::string & error )
    {
        const std::string key = sideKey[index];

        const Json::Value * heroField = side.find( "hero" );
        if ( heroField == nullptr || !heroField->isString() ) {
            error = "'" + key + "' needs a \"hero\" name";
            return false;
        }

        const int heroId = findHeroByName( heroField->asString() );
        if ( heroId == Heroes::UNKNOWN ) {
            error = "'" + key + "': there is no hero called '" + heroField->asString() + "'";
            return false;
        }

        hero = world.GetHeroes( heroId );
        if ( hero == nullptr ) {
            error = "'" + key + "': hero '" + heroField->asString() + "' could not be loaded";
            return false;
        }

        const Json::Value * troops = side.find( "troops" );
        if ( troops == nullptr || !troops->isArray() || troops->items().empty() ) {
            error = "'" + key + "' needs a non-empty \"troops\" array";
            return false;
        }

        if ( troops->items().size() > static_cast<size_t>( hero->GetArmy().Size() ) ) {
            error = "'" + key + "' has " + std::to_string( troops->items().size() ) + " troop entries but an army only holds "
                    + std::to_string( hero->GetArmy().Size() );
            return false;
        }

        hero->GetArmy().Reset( false );

        size_t slot = 0;
        for ( const Json::Value & entry : troops->items() ) {
            const Json::Value * monsterField = entry.find( "monster" );
            const Json::Value * countField = entry.find( "count" );

            if ( monsterField == nullptr || !monsterField->isString() ) {
                error = "'" + key + "': every troop needs a \"monster\" name";
                return false;
            }

            if ( countField == nullptr || !countField->isNumber() || countField->asInt() < 1 ) {
                error = "'" + key + "': troop '" + monsterField->asString() + "' needs a \"count\" of at least 1";
                return false;
            }

            const int monsterId = findMonsterByName( monsterField->asString() );
            if ( monsterId == Monster::UNKNOWN ) {
                error = "'" + key + "': there is no monster called '" + monsterField->asString() + "'";
                return false;
            }

            hero->GetArmy().GetTroop( slot )->Set( Monster( monsterId ), static_cast<uint32_t>( countField->asInt() ) );
            ++slot;
        }

        // Artifacts come before spells: a spell book arriving as an artifact has to be in the bag
        // before the spell list is applied to it.
        if ( const Json::Value * artifacts = side.find( "artifacts" ); artifacts != nullptr ) {
            if ( !giveArtifacts( *artifacts, *hero, key, error ) ) {
                return false;
            }
        }

        if ( const Json::Value * spells = side.find( "spells" ); spells != nullptr ) {
            if ( !giveSpells( *spells, *hero, key, error ) ) {
                return false;
            }
        }

        return true;
    }

    // One JSON line describing how the battle ended, for whatever is driving the run.
    void reportResult( const Battle::Result & result, const Heroes * blue, const Heroes * red )
    {
        Json::Writer report;

        report.startObject();
        report.value( "type", "battle_result" );
        report.value( "winner", result.isAttackerWin() ? "blue" : ( result.isDefenderWin() ? "red" : "none" ) );

        for ( const int index : { 0, 1 } ) {
            const Heroes * hero = ( index == 0 ) ? blue : red;

            report.startObject( sideKey[index] );
            report.value( "hero", hero == nullptr ? "" : hero->GetName() );
            report.value( "survived", hero != nullptr && hero->GetArmy().isValid() );
            report.value( "experience", static_cast<int64_t>( index == 0 ? result.getAttackerExperience() : result.getDefenderExperience() ) );

            report.startArray( "army" );
            if ( hero != nullptr ) {
                for ( size_t slot = 0; slot < static_cast<size_t>( hero->GetArmy().Size() ); ++slot ) {
                    const Troop * troop = hero->GetArmy().GetTroop( slot );

                    if ( troop == nullptr || !troop->isValid() ) {
                        continue;
                    }

                    report.startObject();
                    report.value( "monster", troop->GetName() );
                    report.value( "count", static_cast<int64_t>( troop->GetCount() ) );
                    report.endObject();
                }
            }
            report.endArray();

            report.endObject();
        }

        report.endObject();

        // stdout, one line, so a runner can read it without parsing the game's logging.
        std::printf( "%s\n", report.str().c_str() );
        std::fflush( stdout );
    }
}

bool Scenario::run( const std::string & path, std::string & error )
{
    std::string contents;
    if ( !readFile( path, contents, error ) ) {
        return false;
    }

    Json::Value scenario;
    if ( !Json::parse( contents, scenario, error ) ) {
        error = "'" + path + "' is not valid JSON: " + error;
        return false;
    }

    if ( !scenario.isObject() ) {
        error = "'" + path + "' must contain a JSON object";
        return false;
    }

    // The terrain only changes the backdrop and a few movement modifiers, so it is optional.
    int terrain = Maps::Ground::GRASS;

    if ( const Json::Value * terrainField = scenario.find( "terrain" ); terrainField != nullptr ) {
        if ( !terrainField->isString() ) {
            error = "\"terrain\" must be a name such as \"grass\"";
            return false;
        }

        terrain = findTerrainByName( terrainField->asString() );
        if ( terrain == Maps::Ground::UNKNOWN ) {
            error = "there is no terrain called '" + terrainField->asString() + "'";
            return false;
        }
    }

    // The heroes are read out of the world, so it has to exist before they are looked up.
    world.generateBattleOnlyMap( terrain );

    Heroes * heroes[2]{ nullptr, nullptr };

    for ( const int index : { 0, 1 } ) {
        const Json::Value * side = scenario.find( sideKey[index] );

        if ( side == nullptr || !side->isObject() ) {
            error = std::string( "the scenario needs a \"" ) + sideKey[index] + "\" object";
            return false;
        }

        if ( !buildSide( *side, index, heroes[index], error ) ) {
            return false;
        }
    }

    if ( heroes[0]->GetID() == heroes[1]->GetID() ) {
        error = "both sides use the same hero; pick two different ones";
        return false;
    }

    Settings & conf = Settings::Get();

    conf.GetPlayers().Init( static_cast<PlayerColorsSet>( sideColor[0] ) | static_cast<PlayerColorsSet>( sideColor[1] ) );
    world.InitKingdoms();

    conf.SetCurrentColor( sideColor[0] );

    for ( const int index : { 0, 1 } ) {
        const int control = ( ExternalAI::sideMode( index ) == ExternalAI::ControlMode::HUMAN ) ? CONTROL_HUMAN : CONTROL_AI;

        Players::SetPlayerRace( sideColor[index], heroes[index]->GetRace() );
        Players::SetPlayerControl( sideColor[index], control );

        ExternalAI::bindColorToSide( sideColor[index], index );

        heroes[index]->Recruit( sideColor[index], { index, index } );
        heroes[index]->SetSpellPoints( heroes[index]->GetMaxSpellPoints() );
    }

    world.setUniformTerrain( terrain );

    const Battle::Result result = Battle::Loader( heroes[0]->GetArmy(), heroes[1]->GetArmy(), 1 );

    conf.SetCurrentColor( PlayerColor::NONE );

    reportResult( result, heroes[0], heroes[1] );

    return true;
}
